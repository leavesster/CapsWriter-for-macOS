# coding: utf-8
"""AnnotationService 标注落盘隔离测试（2026-08-23）。

场景：
1. record 正常写 JSONL + 拷贝音频到 audio/；
2. record audio_src=None 不崩、audio_file 为 null；
3. mark_last_problem 无案例拒绝 / 有案例追加 marked 记录 / 重复标记去重；
4. record 落盘异常（root 不可写）不上抛，mark_last_problem 返回 write_failed
   且不置去重标记（可重试）。

测试通过假 app.base_dir 把 root 定位到临时目录，绝不写真实 evals/manual_cases。
用法：项目根目录下 `.venv/bin/python tools/test_editor_annotation.py`
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# 保证从任意 cwd 运行都能导入项目包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeEB:
    def __init__(self):
        self.msgs = []

    def notify(self, m, k):
        self.msgs.append((m, k))


class _FakeState:  # 仅承载 editor_last_case
    def __init__(self):
        self.editor_last_case = None


class _FakeApp:
    def __init__(self):
        self.state = _FakeState()
        self.error_bus = _FakeEB()

    @property
    def base_dir(self):
        return Path(self._tmp)


def _make_svc(base: Path):
    from core.client.output.annotation_store import AnnotationService

    app = _FakeApp()
    app._tmp = str(base)
    return AnnotationService(app), app


def case_record_and_copy(tmp: Path):
    """场景 1：record 正常写 JSONL + 拷贝音频 + 返回含相对路径的 entry"""
    svc, _ = _make_svc(tmp)
    src = tmp / 'fake.mp3'
    src.write_bytes(b'ID3xxxx')
    e1 = svc.record(
        {
            'ts': '2026-08-23T12:00:00',
            'task_id': 'tid-0001',
            'status': 'corrected',
            'raw_text': '原始',
            'final_text': '纠正后',
            'source_app': {'pid': 123, 'name': 'Safari'},
        },
        audio_src=src,
    )
    e2 = svc.record(
        {
            'ts': '2026-08-23T12:01:00',
            'task_id': 'tid-0002',
            'status': 'problem',
            'raw_text': '原始2',
            'final_text': None,
            'source_app': None,
        },
        audio_src=src,
    )
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == 2, f'应有两行 JSONL，实际 {len(lines)}'
    assert json.loads(lines[0])['final_text'] == '纠正后'
    assert json.loads(lines[1])['status'] == 'problem'
    # JSONL 中文不转义（ensure_ascii=False）
    assert '原始' in lines[0]
    assert e1['audio_file'] and (svc.root / e1['audio_file']).exists(), '音频应被拷贝'
    assert e2['audio_file'] and e2['audio_file'] != e1['audio_file'], '两次拷贝文件名应不同'
    print('  case_record_and_copy: PASS')


def case_record_no_audio(tmp: Path):
    """场景 2：audio_src=None 不崩、audio_file 为 null"""
    svc, _ = _make_svc(tmp)
    e = svc.record({'task_id': 'tid-x', 'status': 'corrected', 'raw_text': 'r', 'final_text': 'f'},
                   audio_src=None)
    assert e['audio_file'] is None, 'audio_src=None 时 audio_file 应为 null'
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])['audio_file'] is None
    # audio_src 指向不存在文件同样跳过拷贝
    e2 = svc.record({'task_id': 'tid-y'}, audio_src=tmp / 'nope.mp3')
    assert e2['audio_file'] is None
    print('  case_record_no_audio: PASS')


def case_mark_last_problem(tmp: Path):
    """场景 3：无案例拒绝 / 有案例追加 marked / 重复标记去重 / 发通知"""
    svc, app = _make_svc(tmp)
    src = tmp / 'fake.mp3'
    src.write_bytes(b'ID3xxxx')
    svc.record({'task_id': 'tid-0001', 'status': 'corrected', 'raw_text': 'r', 'final_text': 'f'},
               audio_src=src)

    # 无案例 -> 拒绝
    r0 = svc.mark_last_problem()
    assert r0['ok'] is False and r0['reason'] == 'no_case'
    assert app.error_bus.msgs == [], '拒绝不应发通知'

    # 有案例 -> 追加 marked 记录
    app.state.editor_last_case = {
        'ts': '2026-08-23T12:02:00',
        'task_id': 'tid-0003',
        'raw_text': 'r',
        'final_text': 'f',
        'audio_src': str(src),
        'source_app': None,
    }
    r1 = svc.mark_last_problem()
    assert r1['ok'] is True
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert lines[-1] and json.loads(lines[-1])['status'] == 'marked'
    assert app.error_bus.msgs, '标记成功应发通知'

    # 重复标记 -> 去重
    r2 = svc.mark_last_problem()
    assert r2['ok'] is False and r2['reason'] == 'already_marked'
    lines2 = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines2) == len(lines), '去重时不应再追加记录'
    print('  case_mark_last_problem: PASS')


def case_record_error_swallowed(tmp: Path):
    """场景 4：root 不可写时 record 不上抛、mark 返回 write_failed 且可重试"""
    blocker = tmp / 'blocker'
    blocker.write_bytes(b'not-a-dir')  # base_dir 指向一个文件 -> mkdir 必失败
    svc, app = _make_svc(blocker)
    src = tmp / 'fake.mp3'
    src.write_bytes(b'ID3xxxx')
    # 不上抛
    e = svc.record({'task_id': 'tid-z', 'status': 'corrected', 'raw_text': 'r', 'final_text': 'f'},
                   audio_src=src)
    assert e['audio_file'] is None
    # mark 落盘失败：返回 write_failed，不置去重标记
    app.state.editor_last_case = {
        'ts': '2026-08-23T12:03:00',
        'task_id': 'tid-0009',
        'raw_text': 'r',
        'audio_src': str(src),
        'source_app': None,
        'marked': False,
    }
    r = svc.mark_last_problem()
    assert r['ok'] is False and r['reason'] == 'write_failed', f'实际 {r}'
    assert app.state.editor_last_case['marked'] is False, '落盘失败不应置去重标记'
    assert app.error_bus.msgs == [], '落盘失败不应发成功通知'
    print('  case_record_error_swallowed: PASS')


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        for sub in ('s1', 's2', 's3'):
            (tmp / sub).mkdir()
        print('annotation_store 标注落盘测试：')
        case_record_and_copy(tmp / 's1')
        case_record_no_audio(tmp / 's2')
        case_mark_last_problem(tmp / 's3')
        case_record_error_swallowed(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('annotation_store 全部断言通过 ✅')


if __name__ == '__main__':
    main()
