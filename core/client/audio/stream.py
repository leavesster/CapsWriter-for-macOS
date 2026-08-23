# coding: utf-8
"""
音频流管理模块

提供 AudioStreamManager 类用于管理音频输入流，包括流的创建、
启动、停止和设备检测。
"""

from __future__ import annotations

import sys
import time
import threading
import platform
from typing import TYPE_CHECKING, Optional

import numpy as np
import sounddevice as sd

from core.client.state import console
from . import logger

if TYPE_CHECKING:
    from core.client.state import ClientState
    from ..app import CapsWriterClient



class AudioStreamManager:
    """
    音频流管理器
    
    负责管理音频输入流的生命周期，包括：
    - 检测和选择音频设备
    - 创建和启动音频流
    - 处理音频数据回调
    - 流的重启和关闭
    
    Attributes:
        state: 客户端状态实例
        sample_rate: 采样率（默认 48000Hz）
        block_duration: 每个数据块的时长（秒，默认 0.05s）
    """
    
    SAMPLE_RATE = 48000
    BLOCK_DURATION = 0.05  # 50ms
    
    def __init__(self, app: CapsWriterClient):
        """
        初始化音频流管理器
        
        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._channels = 1
        self._running = False  # 标志是否应该运行
        self._recording_session_count = 0
        self._session_lock = threading.RLock()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state
    
    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags
    ) -> None:
        """
        音频数据回调函数
        
        当音频流接收到新数据时调用，将数据放入异步队列中。
        """
        # 只在录音状态时处理数据
        if not self.state.recording:
            return
        
        import asyncio
        
        # 将数据放入队列
        if self.app.loop and self.state.queue_in:
            enqueue_time = time.time()
            trace_id = self.state.active_trace_id
            audio_data = indata.copy()

            # 记录前几帧音频的能量特征，用于判断当前录音链路里拿到的到底是
            # 真实麦克风波形、近零静音帧，还是异常的全零数据。
            mean_abs = float(np.mean(np.abs(audio_data)))
            rms = float(np.sqrt(np.mean(np.square(audio_data))))
            peak = float(np.max(np.abs(audio_data)))
            zero_ratio = float(np.mean(audio_data == 0.0))
            channels = int(audio_data.shape[1]) if audio_data.ndim > 1 else 1

            # 这里只记录“第一帧真正进入队列”的时刻。
            # 如果后续发现录音任务并不是由按键按下直接驱动，这个点会和按键时间线明显错位。
            self.state.mark_first_audio_enqueue(
                trace_id=trace_id,
                enqueue_time=enqueue_time,
                frames=frames,
            )
            self.state.mark_audio_metrics(
                trace_id=trace_id,
                rms=rms,
                peak=peak,
                mean_abs=mean_abs,
                zero_ratio=zero_ratio,
                channels=channels,
            )
            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'data',
                    'time': enqueue_time,
                    'data': audio_data,
                    'trace_id': trace_id,
                }),
                self.app.loop
            )

    def should_start_immediately(self) -> bool:
        """
        判断当前平台是否需要在客户端启动时立即打开输入流。

        macOS 的新 Caps Lock 方案要求：
        - 客户端空闲时不要长期占用麦克风；
        - 只有长按真正进入录音时，系统左侧麦克风指示才应该出现。
        因此在 Darwin + `remap_f18` 模式下默认走按需开流。
        """
        from config_client import ClientConfig as Config

        if platform.system() != 'Darwin':
            return True

        if getattr(Config, 'macos_caps_mode', 'off') != 'remap_f18':
            return True

        return not getattr(Config, 'macos_caps_open_stream_on_demand', True)

    def start_recording_session(self) -> bool:
        """
        声明一次新的录音会话即将开始。

        返回值语义：
        - `True`：当前录音会话具备可用音频流；
        - `False`：音频流启动失败，本次录音不应继续推进。
        """
        with self._session_lock:
            self._recording_session_count += 1

            if self.should_start_immediately():
                success = self.state.stream is not None or self.start() is not None
                if not success and self._recording_session_count > 0:
                    self._recording_session_count -= 1
                return success

            if self.state.stream is None:
                logger.info("[audio] stream open requested by recording session")
                success = self.start() is not None
                if not success and self._recording_session_count > 0:
                    self._recording_session_count -= 1
                return success

            return True

    def stop_recording_session(self) -> None:
        """
        声明一次录音会话已经结束。

        在 macOS 按需开流模式下，最后一个录音会话结束时立即关闭输入流，
        让系统麦克风占用指示同步消失。
        """
        with self._session_lock:
            if self._recording_session_count > 0:
                self._recording_session_count -= 1

            if self.should_start_immediately():
                return

            if self._recording_session_count == 0 and self.state.stream is not None:
                logger.info("[audio] stream close requested by recording session end")
                self.stop()
    
    def _on_stream_finished(self) -> None:
        """音频流结束回调"""
        if not threading.main_thread().is_alive():
            return
        if not self._running:
            return
        
        logger.info("音频流意外结束，正在尝试重启...")
        self.reopen()
    
    def _reload_portaudio(self) -> None:
        """
        重载 PortAudio，强制刷新音频设备列表与默认输入设备。

        PortAudio 在首次 `import sounddevice` 时会把整张设备列表和默认设备
        索引一次性缓存（`Pa_Initialize`），运行期不会自动刷新。当系统音频拓扑
        发生变化时——例如：
        - 接入/拔出耳机、AirPods、USB 麦克风（默认输入设备被 macOS 切换）；
        - 启动 SoundSource 等使用虚拟音频驱动（ACE/ARK）的软件（设备增删）；
        旧缓存里的设备句柄/默认索引会失效，导致 `device=None` 指向错误或
        失效的设备而录不到音。本方法走 terminate → 重新 dlopen → initialize
        的流程重建缓存，使后续 `query_devices` / `InputStream(device=None)`
        都基于当前真实的设备拓扑与默认输入设备。

        注意：重载会使所有已打开的流失效，因此只应在“当前没有打开着的流”时调用
        （macOS 按需开流模式下每次 start() 时即满足此条件）。
        """
        try:
            sd._terminate()
            sd._ffi.dlclose(sd._lib)
            sd._lib = sd._ffi.dlopen(sd._libname)
            sd._initialize()
        except Exception as e:
            logger.warning(f"重载 PortAudio 时发生警告: {e}")

    def _find_builtin_mic(self) -> Optional[int]:
        """
        在设备列表中查找 Mac 内建麦克风，返回其设备索引。

        临时策略（2026-08-12）：默认输入设备会跟随耳机 / AirPods 等外设自动切换，
        而耳机麦克风收音效果差，用户希望固定使用本机内建麦克风录音。这里按设备名
        匹配内建麦克风：
        - 中文系统：`MacBook Air麦克风`、`MacBook Pro麦克风`、`内建麦克风`
        - 英文系统：`MacBook Air Microphone`、`Built-in Microphone`
        找不到（例如 Mac mini 外接声卡）时返回 None，由调用方回退到默认输入设备。
        """
        try:
            for index, dev in enumerate(sd.query_devices()):
                if dev['max_input_channels'] <= 0:
                    continue
                name = dev.get('name', '')
                if ('内建' in name or 'Built-in' in name
                        or ('麦克风' in name and 'MacBook' in name)
                        or ('Microphone' in name and 'MacBook' in name)):
                    logger.info(f"找到内建麦克风: {name} (index={index})")
                    return index
        except Exception as e:
            logger.warning(f"查找内建麦克风失败: {e}")
        return None

    def start(self) -> Optional[sd.InputStream]:
        """
        启动音频流

        Returns:
            创建的音频输入流，如果失败返回 None
        """
        if self._running:
            logger.debug("音频流已在运行，跳过启动")
            return self.state.stream

        # macOS 按需开流：每次建流前重载 PortAudio，刷新设备列表与默认输入设备。
        # 这样无论用户在系统设置里切换了麦克风、插拔了耳机，还是启动了 SoundSource
        # 等带虚拟音频驱动的软件改变了设备拓扑，设备索引都能保持有效。此处 start()
        # 一定是在“无打开流”状态下被调用，重载是安全的。
        if platform.system() == 'Darwin':
            self._reload_portaudio()

        # 检测音频设备
        # 临时策略（2026-08-12）：macOS 优先使用本机内建麦克风（耳机麦克风收音差），
        # 找不到内建麦克风时回退到系统默认输入设备；其它平台保持跟随默认设备不变。
        device_index = None
        try:
            if platform.system() == 'Darwin':
                device_index = self._find_builtin_mic()
            if device_index is not None:
                device = sd.query_devices(device_index)
                source_desc = '内建麦克风'
            else:
                device = sd.query_devices(kind='input')
                source_desc = '默认音频设备'
            self._channels = min(2, device['max_input_channels'])
            device_name = device.get('name', '未知设备')
            console.print(
                f'使用{source_desc}：[italic]{device_name}，声道数：{self._channels}',
                end='\n\n'
            )
            logger.info(f"找到音频设备: {device_name}, 声道数: {self._channels}")
        except UnicodeDecodeError:
            logger.warning("无法获取音频设备名称（编码问题）")
        except sd.PortAudioError:
            logger.error("未找到麦克风设备")
            input('按回车键退出')
            sys.exit(1)
        
        # 创建音频流
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=int(self.BLOCK_DURATION * self.SAMPLE_RATE),
                device=device_index,  # 内建麦克风索引；None 时跟随系统默认输入设备
                dtype="float32",
                channels=self._channels,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
            try:
                stream.start()
            except Exception:
                # InputStream 构造成功即已向 PortAudio 申请了底层资源；若 start()
                # 失败必须释放已创建的流，否则句柄静默泄漏（2026-08-23 补齐）。
                try:
                    stream.close()
                except Exception as close_err:
                    logger.debug(f"[audio] 回收启动失败的音频流时出错: {close_err}")
                raise

            self.state.stream = stream
            self._running = True
            logger.info("[audio] stream open")
            logger.debug(
                f"音频流已启动: 采样率={self.SAMPLE_RATE}, "
                f"块大小={int(self.BLOCK_DURATION * self.SAMPLE_RATE)}"
            )
            return stream
            
        except Exception as e:
            logger.error(f"创建音频流失败: {e}", exc_info=True)
            return None
    
    def stop(self) -> None:
        """停止音频流"""
        if not self._running:
            return

        self._running = False  # 标记为停止
        self._recording_session_count = 0
        stream = self.state.stream
        self.state.stream = None  # 立即清除引用，允许新录音会话判断流已不可用
        if stream is not None:
            # 关闭动作放到带超时的后台线程执行：即使关闭路径卡死，也不会一直持有
            # _session_lock 导致后续按键全部无响应（2026-06 已验证的兜底策略）。
            def _close():
                try:
                    # 先显式 abort 再 close（2026-08-23 泄漏修复）。
                    # 直接对运行中的流调 close() 时，PortAudio 内部需要先走“优雅停止”
                    # 路径，在 macOS 极短录音、设备状态异常等场景下可能永远卡死；
                    # 一旦卡死，外层 5s 超时放弃后音频流句柄就永久泄漏，表现为系统
                    # 麦克风指示灯（橙灯）常亮、只能重启客户端恢复。
                    # Pa_AbortStream 会立即丢弃 pending 缓冲并停止 IOProc，让后续
                    # close() 只做纯资源释放，从根上规避挂死路径。
                    try:
                        if stream.active:
                            stream.abort()
                    except Exception as abort_err:
                        # 流可能已自行停止/结束，abort 报错不应阻塞后续 close
                        logger.debug(f"[audio] abort 音频流时出现异常（可忽略）: {abort_err}")
                    stream.close()
                except Exception as e:
                    # close() 抛异常同样意味着资源可能未释放，必须以 ERROR 级留痕；
                    # 旧实现用 DEBUG 吞掉异常，是除“close 挂死超时”外的第二个静默
                    # 泄漏口（泄漏时日志毫无痕迹，导致取证困难）。
                    logger.error(
                        f"[audio] 关闭音频流时发生错误，音频流可能泄漏"
                        f"（麦克风指示灯将常亮直至重启）: {e}",
                        exc_info=True,
                    )
                    self._notify_stream_leak()

            t = threading.Thread(target=_close, daemon=True)
            t.start()
            t.join(timeout=5.0)
            if t.is_alive():
                logger.error(
                    "[audio] stream.close() 超时（5s），音频流句柄已泄漏："
                    "系统麦克风指示灯将保持点亮，直到重启 CapsWriter 客户端"
                )
                self._notify_stream_leak()
            else:
                logger.info("[audio] stream close")
                logger.debug("音频流已停止")

    def _notify_stream_leak(self) -> None:
        """音频流句柄泄漏时通知用户。

        泄漏本身不影响后续录音（新录音会开新流），但系统麦克风指示灯会常亮，
        用户需要知情并决定何时重启客户端。通知走 ErrorBus（带去重 key，
        避免多次泄漏时通知轰炸）；ErrorBus 不可用时静默降级。
        """
        try:
            eb = getattr(self.app, 'error_bus', None)
            if eb is not None:
                eb.notify(
                    "音频流关闭失败，麦克风指示灯可能常亮；"
                    "录音仍可继续使用，方便时请重启 CapsWriter（菜单栏或 capswriter restart）",
                    'stream_leak',
                )
        except Exception as e:
            logger.debug(f"[audio] 发送音频流泄漏通知失败: {e}")
    
    def reopen(self) -> Optional[sd.InputStream]:
        """
        重新启动音频流
        
        Returns:
            新创建的音频输入流
        """
        logger.info("正在重启音频流...")
        
        # 停止旧流
        self.stop()

        # 重载 PortAudio，更新设备列表（与 start() 复用同一逻辑）
        self._reload_portaudio()

        # 等待设备稳定
        time.sleep(0.1)
        
        # 启动新流
        return self.start()
