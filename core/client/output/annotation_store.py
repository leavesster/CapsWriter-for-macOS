# coding: utf-8 -*-
"""
标注落盘服务（编辑框标注功能，2026-08-23 创建；2026-08-24 语义重定义）

把「真实语音 + 用户编辑后的正确文本」沉淀为评测数据：
- 音频：从运行期录音目录拷贝到 evals/manual_cases/audio/（防录音目录被清理后标注失效）
- 元数据：追加到 evals/manual_cases/cases.jsonl，一行一案例

status 口径（2026-08-24）：
- corrected       = 编辑框 Enter 确认（raw + 用户编辑的 final 真值）
- final_unreliable= 标记「上一条真值不可靠」：上一条是编辑框确认条，final 不可采信
- raw_unreliable  = 标记「上一条转录有误」：上一条是 Esc 放弃条 / 非编辑框条，
                    只有 raw，标记时才落一条 raw-only 记录
（旧口径 problem/marked 已废除：Esc 放弃不再写任何记录。）

无效条过滤（兜底层，与 result_processor 入口闸门同条件）：
①时长 <0.5s（用户正常短句实测 0.70s 校准）②时长 <2s 且 raw 为空。
命中不入数据集。

线程安全：编辑框回调在主线程、菜单/热键在各自线程调用，统一加锁串行写。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from core.client.output import logger

if TYPE_CHECKING:
    from core.client.app import CapsWriterClient

# 通知摘录长度（字符）
_SNIPPET_LEN = 20


def is_invalid_annotation_case(
    raw_text: str, recording_duration: Optional[float]
) -> bool:
    """严格按 2026-08-24 口径判断标注域无效条。

    该函数是结果入口闸门与落盘兜底共用的唯一判定源，避免两层因边界差异
    让同一条转录出现“可输出却不可标注”的不一致。时长未知仅表示 trace
    缺失，不能仅凭空文本推断为误触发，因此必须保留给既有输出链路处理。
    """
    if recording_duration is None:
        return False
    duration = float(recording_duration)
    return duration < 0.5 or (duration < 2.0 and not (raw_text or '').strip())


class AnnotationService:
    """标注库写入器；实例挂在 CapsWriterClient.annotation 上。"""

    def __init__(self, app: CapsWriterClient):
        self.app = app
        # base_dir = 项目根；标注库固定落在 evals/manual_cases/（evals 结构约定）
        self.root = Path(app.base_dir) / 'evals' / 'manual_cases'
        self.audio_dir = self.root / 'audio'
        self.jsonl_path = self.root / 'cases.jsonl'
        self._lock = threading.Lock()
        # record() 最近一次写入是否成功；mark_last_problem 据此决定是否置去重标记，
        # 写失败时不置位，用户可重试「标记上一条」。锁内写、锁外读（容忍良态竞态）。
        self._last_write_ok = True

    def record(self, case: Dict[str, Any], audio_src: Optional[Path] = None) -> Dict[str, Any]:
        """规范化并追加一条案例；audio_src 存在时拷贝进 audio/。

        无效条（过短/为空，见 is_invalid_annotation_case）直接跳过不入库，
        返回 {'skipped': True}。
        任何异常都不上抛（logger.error 留痕）--标注失败绝不影响正常上屏流程。
        """
        if is_invalid_annotation_case(
            case.get('raw_text'), case.get('recording_duration')
        ):
            logger.info(
                f"[annotation] 无效案例（过短/为空）不入库 task={case.get('task_id')} "
                f"dur={case.get('recording_duration')}")
            return {'skipped': True, 'reason': 'invalid_case'}
        entry: Dict[str, Any] = {
            'ts': case.get('ts') or time.strftime('%Y-%m-%dT%H:%M:%S'),
            'task_id': case.get('task_id'),
            'status': case.get('status', 'corrected'),
            'raw_text': case.get('raw_text'),
            'final_text': case.get('final_text'),
            'recording_duration': case.get('recording_duration'),
            'source_app': case.get('source_app'),
            'mode': case.get('mode', 'editor'),
            'kind': case.get('kind'),
            'audio_file': None,
        }
        try:
            with self._lock:
                if audio_src is not None and Path(audio_src).exists():
                    self.audio_dir.mkdir(parents=True, exist_ok=True)
                    suffix = Path(audio_src).suffix or '.mp3'
                    dst = self.audio_dir / (
                        f"{entry['ts'].replace(':', '').replace('-', '')}_"
                        f"{(entry['task_id'] or 'x')[:8]}{suffix}"
                    )
                    shutil.copy2(audio_src, dst)
                    entry['audio_file'] = f'audio/{dst.name}'
                self.root.mkdir(parents=True, exist_ok=True)
                with self.jsonl_path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                self._last_write_ok = True
        except Exception as e:
            self._last_write_ok = False
            logger.error(f"[annotation] 标注落盘失败（不影响正常输出流程）: {e}")
            return entry
        logger.info(f"[annotation] 已记录案例 status={entry['status']} task={entry['task_id']}")
        return entry

    def mark_last_problem(self) -> Dict[str, Any]:
        """「标记上一条」入口（菜单项 / ⌃⌥M 热键共用）。

        按「上一条」的 kind 分流（2026-08-24 口径）：
        - kind=editor_confirmed（编辑框 Enter 确认条）-> 标记「真值不可靠」，
          追加 final_unreliable 记录（final 不可采信）
        - 其余（Esc 放弃条 / 非编辑框条）-> 标记「转录有误」，
          追加 raw_unreliable 记录（raw-only）
        通知带内容摘录（有 final 用 final，无则 raw），让用户确认标记对象。
        """
        st = getattr(self.app, 'state', None)
        case = getattr(st, 'editor_last_case', None) if st is not None else None
        if not case:
            logger.info('[annotation] 没有可标记的上一条案例')
            return {'ok': False, 'reason': 'no_case'}
        if case.get('marked'):
            return {'ok': False, 'reason': 'already_marked'}

        kind = case.get('kind', 'direct')
        if kind == 'editor_confirmed' and case.get('final_text'):
            status = 'final_unreliable'
            final_text = case.get('final_text')
            notify_msg = '已标记上一条真值不可靠'
        else:
            status = 'raw_unreliable'
            final_text = None
            notify_msg = '已标记上一条转录有误'

        # 标记入口也显式复用唯一判定函数：即使上游误把无效条登记为上一条，
        # 也不能借由手动标记绕过标注域过滤。
        if is_invalid_annotation_case(
            case.get('raw_text'), case.get('recording_duration')
        ):
            return {'ok': False, 'reason': 'invalid_case'}

        res = self.record(
            {
                'ts': case.get('ts'),
                'task_id': case.get('task_id'),
                'status': status,
                'raw_text': case.get('raw_text'),
                'final_text': final_text,
                'recording_duration': case.get('recording_duration'),
                'source_app': case.get('source_app'),
                'mode': case.get('mode', 'direct'),
                'kind': kind,
            },
            audio_src=Path(case['audio_src']) if case.get('audio_src') else None,
        )
        if res.get('skipped'):
            return {'ok': False, 'reason': 'invalid_case'}
        if not self._last_write_ok:
            # 落盘失败：不置去重标记，允许用户重试，保证标记不丢
            return {'ok': False, 'reason': 'write_failed'}
        case['marked'] = True  # 原地置位实现去重（dict 由 state 持有）
        # 通知带内容摘录：有 final 用 final，无则 raw（约 20 字 + …）
        snippet = ((final_text or case.get('raw_text') or '')).strip()
        if snippet:
            snippet = snippet[:_SNIPPET_LEN] + ('…' if len(snippet) > _SNIPPET_LEN else '')
            notify_msg = f'{notify_msg}：{snippet}'
        eb = getattr(self.app, 'error_bus', None)
        if eb is not None:
            try:
                eb.notify(notify_msg, 'mark_last_problem')
            except Exception as e:
                logger.error(f"[annotation] 标记成功但通知发送失败: {e}")
        return {'ok': True}
