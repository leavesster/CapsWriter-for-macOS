# coding: utf-8 -*-
"""AnnotationService 标注落盘隔离测试（2026-08-23 创建，2026-08-24 语义重定义）。

场景：
1. record 正常写 JSONL + 拷贝音频到 audio/；
2. record audio_src=None 不崩、audio_file 为 null；
3. 无效案例过滤（时长 <0.5s / 已知时长 <2s 且空文本）不入库；
   时长未知不能仅凭空文本判成无效条；
4. mark_last_problem 语义分流：editor_confirmed -> final_unreliable（真值不可靠，
   通知摘录取 final）；direct / editor_canceled -> raw_unreliable（转录有误）；
   无案例拒绝 / 重复标记去重 / 通知带内容摘录；
5. record 落盘异常（root 不可写）不上抛，mark_last_problem 返回 write_failed
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
            'kind': 'editor_confirmed',
            'raw_text': '原始',
            'final_text': '纠正后',
            'recording_duration': 5.2,
            'source_app': {'pid': 123, 'name': 'Safari'},
        },
        audio_src=src,
    )
    e2 = svc.record(
        {
            'ts': '2026-08-23T12:01:00',
            'task_id': 'tid-0002',
            'status': 'raw_unreliable',
            'kind': 'direct',
            'raw_text': '原始2',
            'final_text': None,
            'recording_duration': 3.1,
            'source_app': None,
        },
        audio_src=src,
    )
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == 2, f'应有两行 JSONL，实际 {len(lines)}'
    assert json.loads(lines[0])['final_text'] == '纠正后'
    assert json.loads(lines[1])['status'] == 'raw_unreliable'
    # JSONL 中文不转义（ensure_ascii=False）
    assert '原始' in lines[0]
    assert e1['audio_file'] and (svc.root / e1['audio_file']).exists(), '音频应被拷贝'
    assert e2['audio_file'] and e2['audio_file'] != e1['audio_file'], '两次拷贝文件名应不同'
    print('  case_record_and_copy: PASS')


def case_record_no_audio(tmp: Path):
    """场景 2：audio_src=None 不崩、audio_file 为 null"""
    svc, _ = _make_svc(tmp)
    e = svc.record({'task_id': 'tid-x', 'status': 'corrected', 'raw_text': 'r',
                    'final_text': 'f', 'recording_duration': 4.0},
                   audio_src=None)
    assert e['audio_file'] is None, 'audio_src=None 时 audio_file 应为 null'
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])['audio_file'] is None
    # audio_src 指向不存在文件同样跳过拷贝
    e2 = svc.record({'task_id': 'tid-y', 'raw_text': 'r2', 'recording_duration': 4.0},
                    audio_src=tmp / 'nope.mp3')
    assert e2['audio_file'] is None
    print('  case_record_no_audio: PASS')


def case_invalid_filtered(tmp: Path):
    """场景 3：无效案例（过短/为空）不入库（record 层兜底过滤）"""
    svc, _ = _make_svc(tmp)
    # ①时长 <0.5s（即使非空）
    r1 = svc.record({'task_id': 't1', 'raw_text': '嗯', 'recording_duration': 0.3})
    # ②时长 <2s 且空文本
    r2 = svc.record({'task_id': 't2', 'raw_text': '', 'recording_duration': 1.2})
    assert r1.get('skipped') and r2.get('skipped'), \
        f'两个无效案例都应跳过，实际 {r1} {r2}'
    # ③时长未知不能仅凭空文本判成无效条：trace 缺失不是误触发的证据。
    unknown = svc.record({'task_id': 'unknown', 'raw_text': ''})
    assert not unknown.get('skipped'), '时长未知不能仅凭空文本判成无效条'
    # 边界外：>2s 空录音保留（真实收音问题）；0.70s 正常短句保留
    r4 = svc.record({'task_id': 't4', 'raw_text': '', 'recording_duration': 3.0})
    r5 = svc.record({'task_id': 't5', 'raw_text': '可以', 'recording_duration': 0.70})
    assert not r4.get('skipped') and not r5.get('skipped'), '边界外案例应正常入库'
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == 3, f'仅三条合法案例应落盘，实际 {len(lines)} 条'
    print('  case_invalid_filtered: PASS')


def case_mark_last_problem(tmp: Path):
    """场景 4：两种标记语义分流 + 摘录 + 去重 + 拒绝"""
    svc, app = _make_svc(tmp)
    src = tmp / 'fake.mp3'
    src.write_bytes(b'ID3xxxx')

    # 无案例 -> 拒绝
    r0 = svc.mark_last_problem()
    assert r0['ok'] is False and r0['reason'] == 'no_case'
    assert app.error_bus.msgs == [], '拒绝不应发通知'

    # editor_confirmed（编辑框 Enter 确认条）-> final_unreliable + 通知摘录取 final
    app.state.editor_last_case = {
        'ts': '2026-08-23T12:02:00',
        'task_id': 'tid-0003',
        'raw_text': '原始识别文本比较长需要截断的样子',
        'final_text': '用户纠正后的真值文本也比较长需要截断',
        'recording_duration': 5.0,
        'audio_src': str(src),
        'source_app': None,
        'mode': 'editor', 'kind': 'editor_confirmed',
    }
    r1 = svc.mark_last_problem()
    assert r1['ok'] is True, f'实际 {r1}'
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    e = json.loads(lines[-1])
    assert e['status'] == 'final_unreliable', e
    assert e['final_text'] == '用户纠正后的真值文本也比较长需要截断'
    assert app.error_bus.msgs, '标记成功应发通知'
    msg = app.error_bus.msgs[-1][0]
    assert '真值不可靠' in msg and '用户纠正后的真值'[:10] in msg, msg

    # 重复标记 -> 去重
    r2 = svc.mark_last_problem()
    assert r2['ok'] is False and r2['reason'] == 'already_marked'
    n_lines = len(svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines())

    # editor_canceled（Esc 放弃条，只有 raw）-> raw_unreliable + 通知摘录取 raw
    app.state.editor_last_case = {
        'ts': '2026-08-23T12:03:00',
        'task_id': 'tid-0004',
        'raw_text': '被放弃那条的原始转录',
        'final_text': None,
        'recording_duration': 4.0,
        'audio_src': str(src),
        'source_app': None,
        'mode': 'editor', 'kind': 'editor_canceled',
    }
    r3 = svc.mark_last_problem()
    assert r3['ok'] is True, f'实际 {r3}'
    lines = svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()
    e = json.loads(lines[-1])
    assert e['status'] == 'raw_unreliable' and e['final_text'] is None, e
    msg = app.error_bus.msgs[-1][0]
    assert '转录有误' in msg and '被放弃那条' in msg, msg

    # direct（非编辑框条）-> 同样 raw_unreliable
    app.state.editor_last_case = {
        'ts': '2026-08-23T12:04:00',
        'task_id': 'tid-0005',
        'raw_text': '直接输出路径的转录',
        'final_text': '直接输出路径的转录',
        'recording_duration': 6.0,
        'audio_src': str(src),
        'source_app': None,
        'mode': 'direct', 'kind': 'direct',
    }
    r4 = svc.mark_last_problem()
    assert r4['ok'] is True
    e = json.loads(svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()[-1])
    assert e['status'] == 'raw_unreliable', e

    # 无效条（0.3s）作为上一条 -> 拒绝（不入库）
    app.state.editor_last_case = {
        'task_id': 'tid-0006', 'raw_text': '', 'recording_duration': 0.3,
        'kind': 'direct', 'mode': 'direct',
    }
    r5 = svc.mark_last_problem()
    assert r5['ok'] is False and r5['reason'] == 'invalid_case', f'实际 {r5}'
    assert len(svc.jsonl_path.read_text(encoding='utf-8').strip().splitlines()) == n_lines + 2
    print('  case_mark_last_problem: PASS')


def case_record_error_swallowed(tmp: Path):
    """场景 5：root 不可写时 record 不上抛、mark 返回 write_failed 且可重试"""
    blocker = tmp / 'blocker'
    blocker.write_bytes(b'not-a-dir')  # base_dir 指向一个文件 -> mkdir 必失败
    svc, app = _make_svc(blocker)
    src = tmp / 'fake.mp3'
    src.write_bytes(b'ID3xxxx')
    # 不上抛
    e = svc.record({'task_id': 'tid-z', 'status': 'corrected', 'raw_text': 'r',
                    'final_text': 'f', 'recording_duration': 4.0},
                   audio_src=src)
    assert e['audio_file'] is None
    # mark 落盘失败：返回 write_failed，不置去重标记
    app.state.editor_last_case = {
        'ts': '2026-08-23T12:03:00',
        'task_id': 'tid-0009',
        'raw_text': 'r',
        'recording_duration': 4.0,
        'audio_src': str(src),
        'source_app': None,
        'mode': 'direct', 'kind': 'direct', 'marked': False,
    }
    r = svc.mark_last_problem()
    assert r['ok'] is False and r['reason'] == 'write_failed', f'实际 {r}'
    assert app.state.editor_last_case['marked'] is False, '落盘失败不应置去重标记'
    assert app.error_bus.msgs == [], '落盘失败不应发成功通知'
    print('  case_record_error_swallowed: PASS')


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        for sub in ('s1', 's2', 's3', 's4'):
            (tmp / sub).mkdir()
        print('annotation_store 标注落盘测试：')
        case_record_and_copy(tmp / 's1')
        case_record_no_audio(tmp / 's2')
        case_invalid_filtered(tmp / 's3')
        case_mark_last_problem(tmp / 's4')
        case_record_error_swallowed(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('annotation_store 全部断言通过 ✅')


if __name__ == '__main__':
    main()
