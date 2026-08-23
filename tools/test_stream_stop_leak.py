# coding: utf-8
"""
AudioStreamManager.stop() 泄漏修复的隔离冒烟测试（2026-08-23）。

背景：macOS 上「抬手松开后麦克风指示灯常亮、只能重启恢复」的根因是
stream.close() 挂死被 5s 超时放弃 / close 抛异常被 DEBUG 吞掉，两个泄漏口
都不留痕。修复后的 stop() 要求：
1. close 前先对 active 流调用 abort()；
2. close 成功 -> INFO 正常留痕、不发通知；
3. close 挂死 -> 5s 超时后 ERROR 留痕 + ErrorBus 通知；
4. close 抛异常 -> ERROR 留痕 + ErrorBus 通知；
5. start() 失败路径回收已创建的流（本脚本不覆盖，逻辑简单由人审）。

用法：项目根目录下 `.venv/bin/python tools/test_stream_stop_leak.py`
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

# 保证从任意 cwd 运行都能导入项目包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeErrorBus:
    """记录 notify 调用的假 ErrorBus"""

    def __init__(self):
        self.calls = []

    def notify(self, message, key):
        self.calls.append((message, key))


class FakeStreamBase:
    """可编排行为的假 sounddevice.InputStream"""

    def __init__(self, *, close_block=False, close_raise=None):
        self.abort_called = False
        self.close_called = False
        self._close_block = close_block
        self._close_raise = close_raise
        self._never = threading.Event()

    @property
    def active(self):
        return True

    def abort(self):
        self.abort_called = True

    def close(self):
        self.close_called = True
        if self._close_raise is not None:
            raise self._close_raise
        if self._close_block:
            self._never.wait(timeout=60)  # 模拟 Pa_CloseStream 卡死


def build_manager(fake_stream):
    """绕过 __init__ 构造 AudioStreamManager 并注入假依赖"""
    from core.client.audio.stream import AudioStreamManager

    mgr = AudioStreamManager.__new__(AudioStreamManager)
    eb = FakeErrorBus()
    # state 是只读 property（返回 app.state），因此流对象经由 app.state 注入
    mgr.app = SimpleNamespace(
        error_bus=eb,
        state=SimpleNamespace(stream=fake_stream),
    )
    mgr._running = True
    mgr._recording_session_count = 1
    mgr._channels = 1
    return mgr, eb


def case_normal():
    """close 正常：abort 被调用、close 完成无通知"""
    s = FakeStreamBase()
    mgr, eb = build_manager(s)
    t0 = time.perf_counter()
    mgr.stop()
    elapsed = time.perf_counter() - t0
    assert s.abort_called, "close 前必须先 abort active 流"
    assert s.close_called, "close 必须被调用"
    assert mgr.state.stream is None and not mgr._running
    assert eb.calls == [], "正常关闭不应发泄漏通知"
    assert elapsed < 2.0, f"正常关闭不应超时（耗时 {elapsed:.2f}s）"
    print(f"  case_normal: PASS ({elapsed:.3f}s)")


def case_close_hang():
    """close 挂死：5s 超时后发泄漏通知"""
    s = FakeStreamBase(close_block=True)
    mgr, eb = build_manager(s)
    t0 = time.perf_counter()
    mgr.stop()
    elapsed = time.perf_counter() - t0
    assert s.abort_called and s.close_called
    assert 4.5 <= elapsed <= 7.0, f"应等待约 5s 超时（实际 {elapsed:.2f}s）"
    assert len(eb.calls) == 1 and eb.calls[0][1] == 'stream_leak', "挂死必须发 stream_leak 通知"
    print(f"  case_close_hang: PASS ({elapsed:.2f}s, 通知已发)")


def case_close_raise():
    """close 抛异常：发泄漏通知"""
    s = FakeStreamBase(close_raise=RuntimeError("mock pa error"))
    mgr, eb = build_manager(s)
    mgr.stop()
    assert s.abort_called and s.close_called
    assert len(eb.calls) == 1 and eb.calls[0][1] == 'stream_leak'
    print("  case_close_raise: PASS (通知已发)")


def case_double_stop_idempotent():
    """重复 stop 幂等：第二次直接返回不炸"""
    s = FakeStreamBase()
    mgr, eb = build_manager(s)
    mgr.stop()
    mgr.stop()  # _running 已 False，应早退
    assert len(eb.calls) == 0
    print("  case_double_stop_idempotent: PASS")


if __name__ == '__main__':
    print("stream.stop() 泄漏修复冒烟测试：")
    case_normal()
    case_close_hang()
    case_close_raise()
    case_double_stop_idempotent()
    print("全部通过 ✅")
    sys.exit(0)
