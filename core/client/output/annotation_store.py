# coding: utf-8 -*-
"""
标注落盘服务（编辑框标注功能，2026-08-23）

把「真实语音 + 用户编辑后的正确文本」沉淀为评测数据：
- 音频：从运行期录音目录拷贝到 evals/manual_cases/audio/（防录音目录被清理后标注失效）
- 元数据：追加到 evals/manual_cases/cases.jsonl，一行一案例
status 口径：corrected=编辑框确认（含纠正文本）；problem=Esc 取消（有问题未纠正）；
marked=「标记上一条有问题」（无真值，仅负面标记）。
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

        任何异常都不上抛（logger.error 留痕）——标注失败绝不影响正常上屏流程。
        """
        entry: Dict[str, Any] = {
            'ts': case.get('ts') or time.strftime('%Y-%m-%dT%H:%M:%S'),
            'task_id': case.get('task_id'),
            'status': case.get('status', 'corrected'),
            'raw_text': case.get('raw_text'),
            'final_text': case.get('final_text'),
            'recording_duration': case.get('recording_duration'),
            'source_app': case.get('source_app'),
            'mode': case.get('mode', 'editor'),
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
        """「标记上一条有问题」入口（菜单项 / ⌃⌥⌘M 热键共用）。"""
        st = getattr(self.app, 'state', None)
        case = getattr(st, 'editor_last_case', None) if st is not None else None
        if not case:
            logger.info('[annotation] 没有可标记的上一条案例')
            return {'ok': False, 'reason': 'no_case'}
        if case.get('marked'):
            return {'ok': False, 'reason': 'already_marked'}
        audio_src = case.get('audio_src')
        self.record(
            {
                'ts': case.get('ts'),
                'task_id': case.get('task_id'),
                'status': 'marked',
                'raw_text': case.get('raw_text'),
                'final_text': None,
                'recording_duration': case.get('recording_duration'),
                'source_app': case.get('source_app'),
                'mode': case.get('mode', 'direct'),
            },
            audio_src=Path(audio_src) if audio_src else None,
        )
        if not self._last_write_ok:
            # 落盘失败：不置去重标记，允许用户重试，保证标记不丢
            return {'ok': False, 'reason': 'write_failed'}
        case['marked'] = True  # 原地置位实现去重（dict 由 state 持有）
        eb = getattr(self.app, 'error_bus', None)
        if eb is not None:
            try:
                eb.notify('已标记上一条识别结果为有问题', 'mark_last_problem')
            except Exception as e:
                logger.error(f"[annotation] 标记成功但通知发送失败: {e}")
        return {'ok': True}
