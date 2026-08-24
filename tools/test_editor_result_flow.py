# coding: utf-8 -*-
"""编辑框标注结果流隔离回归（2026-08-24 口径）。

本脚本仅以 fake app/state/output/annotation 和 unittest.mock 验证 ResultProcessor
副作用顺序；不启动完整客户端、不访问真实剪贴板、不写真实标注数据。覆盖：无效条
直出隔离、未知时长空文本、direct 登记时机，以及 Enter/Esc 两种面板关闭结果流。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

# 保证从任意 cwd 执行时都导入本项目，而不是环境中同名包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本回归不触发真实剪贴板；测试环境未安装可选的 pyclip 时，先提供最小 API
# 让 ResultProcessor 能被导入，避免无关依赖遮蔽结果流语义断言。
if 'pyclip' not in sys.modules:
    _pyclip_stub = types.ModuleType('pyclip')
    _pyclip_stub.copy = lambda content: None
    _pyclip_stub.paste = lambda: ''
    sys.modules['pyclip'] = _pyclip_stub

# 导入 AudioFileManager 会经由 audio 包初始化触及 sounddevice；本回归禁用音频归档，
# 因此空替身足以通过模块加载，不会代替任何实际录音或设备行为。
if 'sounddevice' not in sys.modules:
    sys.modules['sounddevice'] = types.ModuleType('sounddevice')

from config_client import ClientConfig as Config
from core.protocol import RecognitionMessage


class _FakeErrorBus:
    """只记录通知文本和 key，用于确认误触发通知不会被同 key 聚合。"""

    def __init__(self):
        self.notifications = []

    def notify(self, message, key):
        self.notifications.append((message, key))


class _FakeState:
    """结果流需要的最小 state；登记上一条时写入 events 以验证调用顺序。"""

    def __init__(self):
        object.__setattr__(self, 'events', [])
        object.__setattr__(self, 'traces', {})
        object.__setattr__(self, 'editor_last_case', None)
        self.last_recognition_text = None
        self.paste_target = {'pid': 42, 'name': '目标应用'}
        self.output_texts = []

    def __setattr__(self, name, value):
        if name == 'editor_last_case' and hasattr(self, 'events') and value is not None:
            self.events.append('last_case')
        object.__setattr__(self, name, value)

    def pop_trace_context_by_task_id(self, task_id):
        return self.traces.pop(task_id, None)

    def pop_audio_file(self, task_id):
        return None

    def set_output_text(self, text):
        self.output_texts.append(text)


class _FakeCorrector:
    """让热词阶段保持输入原样，隔离结果流而非热词算法。"""

    def correct(self, text, k):
        return type('Correction', (), {'text': text, 'matchs': [], 'similars': []})()

    def substitute(self, text):
        return text


class _FakeHotword:
    def get_phoneme_corrector(self):
        return _FakeCorrector()

    def get_rule_corrector(self):
        return _FakeCorrector()


class _FakeAnnotation:
    """记录 record 发生点，验证 Enter 必须先存 corrected 再登记上一条。"""

    def __init__(self, events):
        self.events = events
        self.records = []

    def record(self, case, audio_src=None):
        self.events.append('record')
        self.records.append((case, audio_src))
        return {'skipped': False}


class _FakeApp:
    def __init__(self, loop):
        self.loop = loop
        self.state = _FakeState()
        self.error_bus = _FakeErrorBus()
        self.hotword = _FakeHotword()
        self.annotation = _FakeAnnotation(self.state.events)
        self.output = object()
        self.diary = object()


def _message(task_id, text):
    """构造满足协议字段的最终识别消息；时长由 trace 决定而非 message.duration。"""
    return RecognitionMessage(
        task_id=task_id, is_final=True, duration=0.0, time_start=10.0,
        time_submit=11.0, time_complete=12.0, text=text,
    )


def _set_trace(state, task_id, duration):
    """duration=None 模拟 trace 缺项；其余值用入口实际计算的两个时间点表达。"""
    if duration is not None:
        state.traces[task_id] = {
            'trace_id': f'trace-{task_id}', 'recording_start_time': 100.0,
            'finish_requested_time': 100.0 + duration,
        }


async def _new_processor():
    """ResultProcessor 构造时需要运行中的 asyncio loop，故统一在协程中创建。"""
    from core.client.output.result_processor import ResultProcessor

    app = _FakeApp(asyncio.get_running_loop())
    processor = ResultProcessor(app)
    processor._emit_text = AsyncMock()
    return processor, app


async def case_invalid_short_text_direct_and_keeps_last():
    """0.3s 非空：每条通知 key 独立、不开面板、仍直出、旧上一条不被覆盖。"""
    processor, app = await _new_processor()
    old_case = {'task_id': 'old', 'kind': 'direct', 'marked': False}
    app.state.editor_last_case = old_case
    app.state.events.clear()
    _set_trace(app.state, 'short-a', 0.3)
    _set_trace(app.state, 'short-b', 0.3)

    with patch.multiple(Config, editor_mode=True, llm_enabled=False, save_audio=False,
                        hot=False), \
            patch('core.client.output.result_processor.sys.platform', 'darwin'), \
            patch('core.client.output.result_processor.get_active_window_info', return_value={}), \
            patch('core.client.output.edit_panel.present_editor') as present_editor:
        await processor._handle_message(_message('short-a', '嗯'))
        await processor._handle_message(_message('short-b', '啊'))

    assert present_editor.call_count == 0, '无效短录音不得打开编辑框'
    assert processor._emit_text.await_args_list[0].args[0] == '嗯'
    assert processor._emit_text.await_args_list[1].args[0] == '啊'
    assert app.state.editor_last_case is old_case, '无效条不得覆盖旧 editor_last_case'
    keys = [key for _, key in app.error_bus.notifications]
    assert keys == ['invalid_case_short-a', 'invalid_case_short-b'], keys
    assert all('录音时间过短或为空' in message for message, _ in app.error_bus.notifications)
    print('  case_invalid_short_text_direct_and_keeps_last: PASS')


async def case_invalid_empty_keeps_last():
    """1.2s 空文本：不开面板且不覆盖上一条；输出层仍自行处理空文本。"""
    processor, app = await _new_processor()
    old_case = {'task_id': 'old-empty', 'kind': 'direct', 'marked': False}
    app.state.editor_last_case = old_case
    app.state.events.clear()
    _set_trace(app.state, 'empty-short', 1.2)

    with patch.multiple(Config, editor_mode=True, llm_enabled=False, save_audio=False,
                        hot=False), \
            patch('core.client.output.result_processor.sys.platform', 'darwin'), \
            patch('core.client.output.result_processor.get_active_window_info', return_value={}), \
            patch('core.client.output.edit_panel.present_editor') as present_editor:
        await processor._handle_message(_message('empty-short', ''))

    assert present_editor.call_count == 0, '已知短时长空文本不得打开编辑框'
    assert app.state.editor_last_case is old_case, '无效空条不得覆盖旧 editor_last_case'
    assert len(app.error_bus.notifications) == 1
    print('  case_invalid_empty_keeps_last: PASS')


async def case_unknown_empty_enters_editor():
    """时长未知＋空文本不是无效条，允许进入编辑框路径且不发无效通知。"""
    processor, app = await _new_processor()

    with patch.multiple(Config, editor_mode=True, llm_enabled=False, save_audio=False,
                        hot=False), \
            patch('core.client.output.result_processor.sys.platform', 'darwin'), \
            patch('core.client.output.edit_panel.present_editor', return_value=True) as present_editor:
        await processor._handle_message(_message('unknown-empty', ''))

    assert present_editor.call_count == 1, '时长未知空文本应允许进入编辑框'
    assert app.error_bus.notifications == [], '时长未知空文本不得触发无效通知'
    assert processor._emit_text.await_count == 0, '面板接管后不应立即直出'
    print('  case_unknown_empty_enters_editor: PASS')


async def case_direct_registers_after_emit():
    """有效 direct：只有 _emit_text 完成后，才可登记 kind=direct 的上一条。"""
    processor, app = await _new_processor()
    events = app.state.events

    async def _emit(text, paste=None):
        events.append('emit')

    processor._emit_text.side_effect = _emit
    _set_trace(app.state, 'direct', 2.5)
    with patch.multiple(Config, editor_mode=False, llm_enabled=False, save_audio=False,
                        hot=False), \
            patch('core.client.output.result_processor.get_active_window_info', return_value={}):
        await processor._handle_message(_message('direct', '直接输出'))

    assert events == ['emit', 'last_case'], events
    assert app.state.editor_last_case['kind'] == 'direct'
    print('  case_direct_registers_after_emit: PASS')


async def case_editor_confirmed_order():
    """Enter：先 corrected 落盘，再登记，再恢复焦点，最后强制 paste 上屏。"""
    processor, app = await _new_processor()
    events = app.state.events

    async def _emit(text, paste=None):
        events.append(('emit', text, paste))

    processor._emit_text.side_effect = _emit
    processor._save_audio_and_diary = lambda *args: None
    case = {'task_id': 'confirm', 'raw_text': '原文', 'time_start': 10.0, 'mode': 'editor'}
    with patch('core.client.output.edit_panel.activate_app_sync',
               side_effect=lambda target: events.append('activate')):
        await processor._editor_confirmed(case, '修正后', None)

    assert app.annotation.records[0][0]['status'] == 'corrected'
    assert app.annotation.records[0][0]['kind'] == 'editor_confirmed'
    assert events == ['record', 'last_case', 'activate', ('emit', '修正后', True)], events
    assert app.state.editor_last_case['kind'] == 'editor_confirmed'
    print('  case_editor_confirmed_order: PASS')


async def case_editor_canceled_clipboard_only():
    """Esc：不落标注、不自动上屏，非空文本仅复制，并登记 editor_canceled。"""
    processor, app = await _new_processor()
    processor._save_audio_and_diary = lambda *args: None
    case = {'task_id': 'cancel', 'raw_text': '原文', 'time_start': 10.0, 'mode': 'editor'}

    with patch('core.client.clipboard.clipboard.safe_copy', return_value=True) as safe_copy:
        await processor._editor_canceled(case, None, '保留在剪贴板')

    assert app.annotation.records == [], 'Esc 不得写 annotation.record'
    safe_copy.assert_called_once_with('保留在剪贴板')
    assert processor._emit_text.await_count == 0, 'Esc 不得自动上屏'
    assert app.state.editor_last_case['kind'] == 'editor_canceled'
    assert app.state.output_texts == ['保留在剪贴板']
    print('  case_editor_canceled_clipboard_only: PASS')


async def main():
    await case_invalid_short_text_direct_and_keeps_last()
    await case_invalid_empty_keeps_last()
    await case_unknown_empty_enters_editor()
    await case_direct_registers_after_emit()
    await case_editor_confirmed_order()
    await case_editor_canceled_clipboard_only()
    print('editor 结果流全部断言通过 ✅')


if __name__ == '__main__':
    asyncio.run(main())
