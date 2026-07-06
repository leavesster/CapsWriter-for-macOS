# coding: utf-8
"""
WebSocket 接收处理模块

处理客户端发送的音频数据，进行分段和缓冲，提交到识别队列。
"""

import json
import time
from base64 import b64decode

import websockets

from ..state import console
from ..schema import Task
from core.protocol import AudioMessage
from core.constants import AudioFormat
from core.tools.my_status import Status
from config_server import ServerConfig as Config
from .. import logger


# 麦克风接收状态指示器
status_mic = Status('正在接收音频', spinner='point')


def _use_qwen_mlx_runner_path() -> bool:
    """
    判断当前服务端是否应启用 Qwen3-ASR Runner 喂音频路径。

    只有 `qwen_asr_mlx` 走这条分叉；其它后端继续使用旧的 60 秒分段 + overlap +
    TaskPipeline 拼接机制，避免为了 macOS MLX 调优影响 Windows / GGUF 稳定基线。
    """
    return Config.model_type.lower() == 'qwen_asr_mlx'


class AudioCache:
    """
    音频缓冲区

    用于缓存接收到的音频数据，直到达到分段阈值后提交处理。
    """
    def __init__(self):
        self.chunks: bytes = b''    # 音频数据缓冲
        self.offset: float = 0.0    # 当前偏移时间（秒）
        self.byte_count: int = 0    # 累计接收字节数

    @property
    def duration(self) -> float:
        """缓冲区音频时长（秒）"""
        return AudioFormat.bytes_to_seconds(len(self.chunks))

    @property
    def total_duration(self) -> float:
        """累计接收的音频总时长（秒）"""
        return AudioFormat.bytes_to_seconds(self.byte_count)

    def reset(self) -> None:
        """重置缓冲区"""
        self.chunks = b''
        self.offset = 0.0
        self.byte_count = 0


async def message_handler(websocket, msg: AudioMessage, cache: AudioCache, app) -> None:
    """
    处理客户端发送的音频消息

    根据消息中的分段参数，将音频数据分段后提交到识别队列。
    """
    queue_in = app.state.queue_in

    global status_mic
    # 旧分段路径看 chunks，新 Runner 路径看 byte_count；两者合并判断可避免
    # qwen_asr_mlx 文件转录时每个音频包都被误判为“首包”。
    is_start = not bool(cache.chunks) and cache.byte_count == 0
    socket_id = str(websocket.id)

    # 从消息中获取分段参数
    seg_threshold = msg.seg_duration + msg.seg_overlap * 2

    try:
        # base64 解码音频数据（float32, 16kHz, mono）
        data = b64decode(msg.data)
        if _use_qwen_mlx_runner_path():
            await _submit_qwen_mlx_runner_patch(
                websocket=websocket,
                msg=msg,
                cache=cache,
                queue_in=queue_in,
                data=data,
                socket_id=socket_id,
                is_start=is_start,
            )
            return

        cache.chunks += data
        cache.byte_count += len(data)

        if not msg.is_final:
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.start()
            if msg.source == 'file' and is_start:
                console.print('正在接收音频文件...')
                logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")

            # 若缓冲已达到分段阈值，将片段作为任务提交
            segment_bytes = AudioFormat.seconds_to_bytes(msg.seg_duration + msg.seg_overlap)
            stride_bytes = AudioFormat.seconds_to_bytes(msg.seg_duration)

            while cache.duration >= seg_threshold:
                segment_data = cache.chunks[:segment_bytes]
                cache.chunks = cache.chunks[stride_bytes:]

                task = Task(
                    source=msg.source,
                    data=segment_data,
                    offset=cache.offset,
                    task_id=msg.task_id,
                    socket_id=socket_id,
                    overlap=msg.seg_overlap,
                    is_final=False,
                    time_start=msg.time_start,
                    time_submit=time.time(),
                    context=msg.context,
                    language=msg.language,
                )
                cache.offset += msg.seg_duration
                queue_in.put(task)
                logger.debug(
                    f"提交音频片段，任务ID: {msg.task_id}, "
                    f"偏移: {cache.offset}s, 缓冲区: {len(cache.chunks)} bytes"
                )

        else:  # is_final
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.stop()
            elif msg.source == 'file':
                print(f'音频文件接收完毕，时长 {cache.total_duration:.2f}s')
                logger.info(f"音频文件接收完毕，任务ID: {msg.task_id}, 时长: {cache.total_duration:.2f}s")

            # 提交最终片段
            task = Task(
                source=msg.source,
                data=cache.chunks,
                offset=cache.offset,
                task_id=msg.task_id,
                socket_id=socket_id,
                overlap=msg.seg_overlap,
                is_final=True,
                time_start=msg.time_start,
                time_submit=time.time(),
                context=msg.context,
                language=msg.language,
            )
            queue_in.put(task)
            logger.debug(f"提交最终片段，任务ID: {msg.task_id}, 数据大小: {len(cache.chunks)} bytes")

            # 重置缓冲区
            cache.reset()

    except Exception as e:
        logger.error(f"音频数据处理错误，任务ID: {msg.task_id}: {e}", exc_info=True)
        raise


async def _submit_qwen_mlx_runner_patch(
    *,
    websocket,
    msg: AudioMessage,
    cache: AudioCache,
    queue_in,
    data: bytes,
    socket_id: str,
    is_start: bool,
) -> None:
    """
    qwen_asr_mlx 专用提交路径：传输包直接变成 Runner 音频增量。

    这里不再使用 `seg_duration + seg_overlap * 2` 阈值，也不再把 60 秒片段当作
    ASR 语义单元；完整任务的切分、推理和拼接统一交给 package Runner。
    """
    global status_mic

    if not msg.is_final:
        if msg.source == 'mic':
            status_mic.start()
        if msg.source == 'file' and is_start:
            console.print('正在接收音频文件...')
            logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")

        offset = cache.total_duration
        cache.byte_count += len(data)
        task = Task(
            source=msg.source,
            data=data,
            offset=offset,
            task_id=msg.task_id,
            socket_id=socket_id,
            overlap=0.0,
            is_final=False,
            time_start=msg.time_start,
            time_submit=time.time(),
            context=msg.context,
            language=msg.language,
        )
        queue_in.put(task)
        logger.debug(
            f"提交 Qwen MLX Runner 音频增量，任务ID: {msg.task_id}, "
            f"offset={offset:.2f}s, bytes={len(data)}"
        )
        return

    if msg.source == 'mic':
        status_mic.stop()
    elif msg.source == 'file':
        print(f'音频文件接收完毕，时长 {cache.total_duration:.2f}s')
        logger.info(f"音频文件接收完毕，任务ID: {msg.task_id}, 时长: {cache.total_duration:.2f}s")

    offset = cache.total_duration
    cache.byte_count += len(data)
    task = Task(
        source=msg.source,
        data=data,
        offset=offset,
        task_id=msg.task_id,
        socket_id=socket_id,
        overlap=0.0,
        is_final=True,
        time_start=msg.time_start,
        time_submit=time.time(),
        context=msg.context,
        language=msg.language,
    )
    queue_in.put(task)
    logger.debug(
        f"提交 Qwen MLX Runner final，任务ID: {msg.task_id}, "
        f"总时长={cache.total_duration:.2f}s, final_bytes={len(data)}"
    )
    cache.reset()


async def ws_recv(websocket, app) -> None:
    """
    WebSocket 接收主函数

    处理单个客户端连接，接收音频数据并分发处理。
    """
    global status_mic

    # 登记 socket 到连接池
    state = app.state
    sockets = state.sockets
    sockets_id = state.sockets_id
    socket_id = str(websocket.id)
    sockets[socket_id] = websocket
    sockets_id.append(socket_id)
    remote = websocket.remote_address
    console.print(f'[bold green]客户端已连接: {remote[0]}:{remote[1]}[/bold green]\n')
    logger.info(f"新客户端连接: {websocket}, ID: {socket_id}")

    # 创建音频缓冲区
    cache = AudioCache()

    # 接收并处理消息
    try:
        async for raw_message in websocket:
            # 使用协议类解析消息
            try:
                data = json.loads(raw_message)
                msg = AudioMessage.from_dict(data)
                # 处理音频数据
                await message_handler(websocket, msg, cache, app)
            except Exception as e:
                logger.error(f"消息解析失败: {str(e)}")
                continue

        logger.info(f"客户端正常关闭连接: {socket_id}")

    except websockets.ConnectionClosed:
        console.print("ConnectionClosed...")
        logger.warning(f"客户端连接已关闭: {socket_id}")
    except websockets.InvalidState:
        console.print("InvalidState...")
        logger.error(f"WebSocket 状态异常: {socket_id}")
    except Exception as e:
        console.print("Exception:", e)
        logger.error(f"WebSocket 接收异常，客户端ID {socket_id}: {e}", exc_info=True)
    finally:
        # 清理资源
        status_mic.stop()
        status_mic.on = False
        sockets.pop(socket_id, None)
        if socket_id in sockets_id:
            sockets_id.remove(socket_id)

        console.print(f'[bold red]客户端已断开: {remote[0]}:{remote[1]}[/bold red]\n')

        # 注意：session 清理由 TaskHandler 在子进程中定期执行
        # （通过检查 sockets_id 判断客户端是否已断开）
        logger.debug(f"客户端资源已清理: {socket_id}")
