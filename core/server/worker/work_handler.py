# coding: utf-8
"""
识别工作单元处理器

负责监听工作单元队列、执行识别流水线并将结果返回主进程。

公平调度：从不同客户端（socket）轮转取工作单元处理，防止文件转录淹没队列。
同 socket 内保持 FIFO 顺序，跨 socket 间轮转调度。
"""

from collections import OrderedDict, deque
from multiprocessing import Queue
from multiprocessing.managers import ListProxy
import queue
from .work_pipeline import WorkPipeline
from ..state import WorkerState
from . import logger


class WorkBuffer:
    """按 task_id 分组缓冲工作单元，支持跨 session 轮转出队。"""
    def __init__(self, state: WorkerState):
        self.state = state
        self._buffers: OrderedDict[str, deque] = OrderedDict()

    def enqueue(self, work):
        """将工作单元放入对应 task_id 的缓冲尾部（同 session 内 FIFO）。
        首次遇到新 task_id 时预创建 session。"""
        tid = work.task_id
        if tid not in self._buffers:
            self._buffers[tid] = deque()
            self.state.get_session(tid, work.socket_id, work.source)
        self._buffers[tid].append(work)

    def pop(self):
        """取出最新 session 的下一个工作单元。没有待处理项时返回 None。"""
        if not self._buffers:
            return None

        tid, buf = next(reversed(self._buffers.items()))
        work = buf.popleft()

        if not buf:
            del self._buffers[tid]

        return work

    def cleanup_works(self):
        """清理已断开连接 session 的缓冲工作单元。"""
        for tid in list(self._buffers):
            if tid not in self.state.sessions:
                logger.debug(f"清理断开连接的 session: {tid[:8]}")
                del self._buffers[tid]

    @property
    def is_empty(self) -> bool:
        return len(self._buffers) == 0


class WorkHandler:
    """
    工作单元处理器

    协调输入输出队列与识别引擎之间的工作单元流。
    支持跨 socket 公平轮转调度。
    """
    def __init__(self, queue_in: Queue, queue_out: Queue, sockets_id: ListProxy, state: WorkerState):
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.sockets_id = sockets_id
        self.state = state

        self.recognizer = None
        self.punc_model = None
        self.aligner = None
        self.pipeline = None

        self.buffer = WorkBuffer(state)

    def set_engine(self, recognizer, punc_model=None, aligner=None):
        """注入识别引擎实例并初始化管线"""
        self.recognizer = recognizer
        self.punc_model = punc_model
        self.aligner = aligner
        # 所有后端统一走 WorkPipeline。公开的 MLX v0.3.5 只提供 Session API，
        # 因此不能依赖上游仓库中未公开的 task runner 扩展。
        self.pipeline = WorkPipeline(recognizer, punc_model, aligner, self.state)

    def drain_queue(self) -> bool:
        """Drain 队列中所有工作单元到缓冲区。Returns: False = 退出信号。"""
        while True:
            # 获取工作单元
            try:
                if self.buffer.is_empty:
                    work = self.queue_in.get(timeout=1)
                else:
                    work = self.queue_in.get(timeout=0.02)
            except queue.Empty:
                if self.buffer.is_empty:
                    self.cleanup_engines()
                    continue
                else:
                    return True
            except InterruptedError:
                continue

            # 判断退出信号
            if work is None:
                return False

            # 跳过已断开连接客户端的工作单元
            if work.socket_id not in self.sockets_id:
                logger.debug(f"跳过断连客户端工作单元: {work.task_id[:8]}")
                continue

            # 工作单元进入缓冲区
            self.buffer.enqueue(work)

    def cleanup(self):
        """清理断连 socket 的缓冲工作单元和 session。"""
        self.state.cleanup_sessions(self.sockets_id)
        self.buffer.cleanup_works()

    def cleanup_engines(self):
        """时间戳引擎空闲时自动卸载。"""
        if self.pipeline and self.pipeline.aligner:
            self.pipeline.aligner.check_idle()

    def loop(self):
        """核心工作循环：drain 队列 → 清理断连 → 轮转执行一个工作单元。"""
        logger.info("WorkHandler 开始工作循环 (公平调度)")

        while True:
            try:
                if not self.drain_queue():
                    break

                work = self.buffer.pop()
                if work is None:
                    continue

                # 安全网：在 pipeline 处理前再次检查（工作单元可能在 drain→pop 之间成为孤儿）
                # if work.socket_id not in self.sockets_id:
                #     logger.debug(f"跳过断连客户端工作单元(安全网): {work.task_id[:8]}")
                #     self.cleanup()
                #     continue

                result = self.pipeline.process(work)
                if result is None:
                    self.cleanup()
                    continue

                self.queue_out.put(result)
                if result.is_final:
                    self.state.sessions.pop(work.task_id, None)
                self.cleanup()
            except InterruptedError:
                continue
            except Exception as e:
                logger.error(f"工作单元执行出错: {str(e)}", exc_info=True)

        logger.info("WorkHandler 工作循环结束")
