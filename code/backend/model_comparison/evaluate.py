"""Post-inference scoring; never imported by the inference entry point."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median

from app.client import write_json
from app.config import ROOT
from model_comparison.run import digest, protected_hashes

GROUPS = ['deepseek_pipeline', 'gpt_pipeline', 'gpt_dense']
NAMES = {'deepseek_pipeline': 'DeepSeek 原流程', 'gpt_pipeline': 'GPT 原流程', 'gpt_dense': 'GPT 密集帧直判'}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def raw_objects(result, group):
    return result.get('objects', []) if group.endswith('_dense') else result.get('items', [])


def inspect(root):
    overview = {}
    for group in GROUPS:
        overview[group] = {}
        for i in range(1, 11):
            video = f'v{i}'
            path = root / group / video / 'result.json'
            if not path.exists():
                continue
            result = load(path)
            overview[group][video] = {'status': result['status'], 'objects': [{
                'id': obj.get('object_id', obj.get('candidate_id')),
                **{k: obj.get(k) for k in ('name', 'description', 'category', 'localization_status',
                                          'identity_uncertain', 'readable_text', 'evidence_refs', 'evidence_frame_ids')}}
                for obj in raw_objects(result, group)], 'events': result.get('events', result.get('event_candidates', [])),
                'net': result.get('net_changes', result.get('operation_analysis', {}).get('net_changes', [])),
                'pending': result.get('operation_analysis', {}).get('pending', []),
                'errors': result.get('errors', result.get('error'))}
    write_json(root / 'review-inputs.json', overview)
    print(json.dumps(overview, ensure_ascii=False, indent=2))


def score_case(result, case, review, group):
    objects = raw_objects(result, group)
    mapping = review['mapping']
    predicted_ids = {obj.get('object_id', obj.get('candidate_id')) for obj in objects}
    if set(mapping) != predicted_ids:
        raise ValueError('Review must cover every new object ID: ' + result['video'])
    expected_ids = set(case['net'])
    counts = Counter(mapping.values())
    unique_correct = {key for key, count in counts.items() if key in expected_ids and count == 1}
    instances_exact = len(objects) == len(expected_ids) and unique_correct == expected_ids
    recognition_exact = instances_exact and not review['basic_label_errors']
    if group.endswith('_dense'):
        events = [(e['kind'], mapping.get(e['object_id'], 'unmapped')) for e in result.get('events', [])]
        nets = [(mapping.get(n['object_id'], 'unmapped'), n['change']) for n in result.get('net_changes', [])]
        abstained = any(obj['identity_uncertain'] for obj in objects) or any(n[1] == 'unknown' for n in nets) or any(e[0] == 'uncertain' for e in events)
    else:
        events = [(e['kind'], mapping.get(cid, 'unmapped')) for e in result.get('event_candidates', []) for cid in e['candidate_ids']]
        operation = result.get('operation_analysis', {})
        nets = [(mapping.get(n['candidate_id'], 'unmapped'), n['change']) for n in operation.get('net_changes', [])]
        abstained = operation.get('status') != 'consistent'
    auxiliary_moves = [event for event in events if event[0] == 'move']
    events = [event for event in events if event[0] != 'move']
    expected_events = [tuple(e) for e in case['history']]
    event_exact = events == expected_events if case['ordered'] else Counter(events) == Counter(expected_events)
    expected_net = list(case['net'].items())
    net_exact = Counter(nets) == Counter(expected_net)
    event_tp = sum((Counter(events) & Counter(expected_events)).values())
    net_tp = sum((Counter(nets) & Counter(expected_net)).values())
    correct = bool(result['status'] == 'completed' and recognition_exact and event_exact and net_exact)
    return {'video': result['video'], 'instances_exact': instances_exact, 'recognition_exact': recognition_exact, 'events_exact': event_exact,
            'net_exact': net_exact, 'complete_correct': correct, 'abstained': abstained,
            'usable_correct': correct and not abstained,
            'unsafe_wrong': not correct and not abstained and result['status'] == 'completed',
            'predicted_events': events, 'expected_events': expected_events, 'predicted_net': nets,
            'auxiliary_moves_not_scored': auxiliary_moves,
            'expected_net': expected_net, 'instance_count': len(objects), 'expected_instance_count': len(expected_ids),
            'event_tp': event_tp, 'event_predictions': len(events), 'event_expected': len(expected_events),
            'net_tp': net_tp, 'net_predictions': len(nets), 'net_expected': len(expected_net),
            'errors': result.get('errors', result.get('error')), 'review_notes': review.get('notes', []),
            'basic_label_errors': review['basic_label_errors'],
            'result_sha256': review['result_sha256']}


def f1(tp, predicted, expected):
    return round(2 * tp / (predicted + expected), 4) if predicted + expected else 0


def freeze(root):
    destination = root / 'manual-review.json'
    if destination.exists():
        raise ValueError('Review already frozen; preserve existing annotation evidence')
    reviews = load(root / 'review-mappings.json')
    for group in GROUPS:
        for i in range(1, 11):
            name = f'v{i}'
            path = root / group / name / 'result.json'
            review = reviews[group][name]
            expected = {o.get('object_id', o.get('candidate_id')) for o in raw_objects(load(path), group)}
            if set(review['mapping']) != expected:
                raise ValueError('Incomplete mapping: ' + group + '/' + name)
            review['result_sha256'] = digest(path)
    write_json(destination, reviews)
    write_json(root / 'experiment-code-hashes.json', {p.name: digest(p) for p in Path(__file__).parent.glob('*.py')})
    print('Reviews frozen for 30 predictions; no inference requests made.')


def evaluate(root):
    labels = load(ROOT.parent / 'datasets/action-regression.json')['cases']
    reviews = load(root / 'manual-review.json')
    report = {'scope': 'Development-set single-run visual comparison; no blind or repeated-run accuracy claim.',
              'scoring': 'New IDs manually mapped using current crops/frames; brand and variant optional. Ordered history required for v7.',
              'groups': {}, 'limits': ['Only 10 existing development videos, one inference per arm.',
                  'No ASR/date/task/delivery tests; all inventory writes disabled.',
                  'Dense arm changes both workflow and frame presentation; no DeepSeek dense arm to isolate their effects.',
                  'Gateway model labels and token counts are vendor-reported, underlying identity not independently verified.',
                  'Pipeline safety guards and direct predictions have different abstention mechanisms.']}
    for group in GROUPS:
        cases = []
        for i in range(1, 11):
            name = f'v{i}'
            path = root / group / name / 'result.json'
            review = reviews[group][name]
            if digest(path) != review['result_sha256']:
                raise ValueError('Predictions changed after annotation: ' + str(path))
            cases.append(score_case(load(path), labels[name], review, group))
        logs = [json.loads(line) for path in (root / group).glob('api*/api-attempts.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]
        success = [row for row in logs if row['status'] == 'success']
        summary = {key: sum(c[key] for c in cases) for key in (
            'instances_exact', 'recognition_exact', 'events_exact', 'net_exact', 'complete_correct', 'usable_correct', 'unsafe_wrong', 'abstained')}
        summary.update(videos=len(cases), http_success=len(success), http_failed=sum(r['status'] == 'failed' for r in logs),
            invalid_outputs=sum(r['status'] == 'invalid_output' for r in logs),
            tokens=sum((r.get('usage') or {}).get('total_tokens', 0) for r in success),
            input_tokens=sum((r.get('usage') or {}).get('prompt_tokens', 0) for r in success),
            output_tokens=sum((r.get('usage') or {}).get('completion_tokens', 0) for r in success),
            http_elapsed_s=round(sum(r['elapsed_s'] for r in logs if r['status'] in ('success', 'failed')), 2),
            actual_models=sorted({r['actual_model'] for r in success if r.get('actual_model')}),
            event_micro_f1=f1(sum(c['event_tp'] for c in cases), sum(c['event_predictions'] for c in cases), sum(c['event_expected'] for c in cases)),
            net_micro_f1=f1(sum(c['net_tp'] for c in cases), sum(c['net_predictions'] for c in cases), sum(c['net_expected'] for c in cases)))
        durations = [load(root / group / f'v{i}/result.json')['elapsed_s'] for i in range(1, 11)]
        summary['median_video_elapsed_s'] = round(median(durations), 2)
        summary['total_video_elapsed_s'] = round(sum(durations), 2)
        summary['subsets'] = {difficulty: {'videos': len(names), 'complete_correct': sum(cases[int(n[1:]) - 1]['complete_correct'] for n in names)}
            for difficulty, names in {'simple': ['v1', 'v8', 'v9', 'v10'], 'medium': ['v2', 'v5'], 'hard': ['v3', 'v4', 'v6', 'v7']}.items()}
        report['groups'][group] = {'summary': summary, 'cases': cases}
    manifest = load(root / 'manifest.json')
    after = protected_hashes()
    report['isolation_unchanged'] = after == manifest['protected_before']
    report['pipeline_input_comparison'] = {}
    for i in range(1, 11):
        name = f'v{i}'
        paths = [root / g / name for g in GROUPS[:2]]
        media = [load(p / 'media.json') for p in paths]
        frame_signatures = [[(f['id'], f['media_time_ms'], digest(p / f['model_file']))
                             for f in m['frames']] for p, m in zip(paths, media)]
        results = [load(p / 'result.json') for p in paths]
        report['pipeline_input_comparison'][name] = {
            'recognition_frame_bytes_and_times_equal': frame_signatures[0] == frame_signatures[1],
            'prompt_hashes_equal': results[0]['prompt_hashes'] == results[1]['prompt_hashes'],
            'dense_frame_count': load(root / 'gpt_dense' / name / 'result.json')['frame_count']}
    write_json(root / 'comparison.json', report)
    lines = ['# 三组模型与流程对比实测', '', '测试日期：2026-10-17。10 段现有开发视频，每组一轮。以下为视觉任务，不包含语音、日期、日程或库存提交。', '',
        '## 汇总', '', '| 指标 | DeepSeek 原流程 | GPT 原流程 | GPT 密集帧直判 |', '| --- | --- | --- | --- |']
    for label, key in [('实物对应完整正确', 'instances_exact'), ('基础认物完整正确（含对应）', 'recognition_exact'), ('动作完整正确（v7 含顺序）', 'events_exact'),
                       ('净变化完整正确', 'net_exact'), ('三项同时正确', 'complete_correct'),
                       ('正确且动作/直判无阻塞', 'usable_correct'), ('错误且动作/直判无阻塞', 'unsafe_wrong')]:
        lines.append('| ' + label + ' | ' + ' | '.join(f'{report["groups"][g]["summary"][key]}/10' for g in GROUPS) + ' |')
    for label, key in [('HTTP 成功调用', 'http_success'), ('HTTP 失败尝试', 'http_failed'),
                       ('结构/引用校验拒绝', 'invalid_outputs'), ('成功请求报告的 tokens', 'tokens'),
                       ('请求累计耗时（秒）', 'http_elapsed_s'), ('单视频耗时中位数（秒）', 'median_video_elapsed_s'),
                       ('动作 micro F1', 'event_micro_f1'), ('净变化 micro F1', 'net_micro_f1')]:
        lines.append('| ' + label + ' | ' + ' | '.join(str(report['groups'][g]['summary'][key]) for g in GROUPS) + ' |')
    lines += ['', '## 逐视频', '', '每格为“基础认物 / 动作 / 净变化”；✓ 表示该项完整匹配，× 表示存在错漏。', '',
              '| 视频 | DeepSeek 原流程 | GPT 原流程 | GPT 密集帧直判 |', '| --- | --- | --- | --- |']
    for i in range(10):
        values = []
        for group in GROUPS:
            case = report['groups'][group]['cases'][i]
            values.append(' / '.join('✓' if case[k] else '×' for k in ('recognition_exact', 'events_exact', 'net_exact')))
        lines.append(f'| v{i + 1} | ' + ' | '.join(values) + ' |')
    lines += ['', '## 评分与边界', '',
        '- 人工评分只在推理后读取答案；每组新 ID 依据当前截图/原文复核，不复用旧 candidate 映射。映射及结果 SHA-256 见 manual-review.json。',
        '- 基础判分接受正确大类；品牌、口味、容量不是门槛。饮料被叫成蜂蜜仍是基础认物错误；映射依据实际物体，动作可独立得分。混框导致无法唯一对应、重复实物、漏动作、把取出放回算新增仍为错误。',
        '- 此处认物评价大类与目标对应，不是框的定位精度。目标可辨但crop含邻物时记录质量问题，不直接抹去已正确的大类/动作；无法唯一对应的多件候选记ambiguous。没有框真值，不报告mAP。',
        '- 人工答案没有穷尽抽屉内部的挪动，三组统一排除 move 后评价 add/remove/return；额外 move 单独保留，不计对错。具体毫秒边界未标注，不报告时间精度。',
        '- “三项同时正确”描述答案正确；无阻塞仅针对动作分析或直判，不代表满足整个入库服务条件，更不是已写库。待确认减少不自动等于正确率提高。',
        '- DeepSeek 与 GPT 原流程的代码、提示词、认物选帧上限一致；GPT 经 Responses、low reasoning、high image detail 和额外推理 token 预算调用。',
        '- 密集组为 4fps 顺序完整帧，原流程已有 8fps 局部动作取帧；密集组不是在所有阶段都更高采样率。',
        '- 未做 DeepSeek 密集组，因此无法把第三组收益单独归因于模型或帧密度。10 条是开发集，单轮结果不是泛化准确率。',
        '- 模型名和 tokens 来自网关；失败/超时请求可能没有用量回执，tokens 不是保证完整的计费总额。未核验底层身份与单价，不虚构费用。请求耗时是累计量，包含重试且三组可能并行。',
        '- 正常项目配置、正式库与应用代码哈希保持不变：' + str(report['isolation_unchanged']) + '。',
        '- 两个原流程组的认物输入帧字节/时间与提示词哈希一致：' + str(all(
            v['recognition_frame_bytes_and_times_equal'] and v['prompt_hashes_equal']
            for v in report['pipeline_input_comparison'].values())) + '。后续取帧调用可能随模型规划不同而变化。',
        '', '## 证据索引', '', 'manifest.json：输入/配置哈希；comparison.json：完整评分；manual-review.json：人工对应及备注；各组 api/api-attempts.jsonl：调用/用量/失败；各视频 result.json：预测；gpt_dense/*/frames.json：密集帧清单。', '']
    (root / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({g: v['summary'] for g, v in report['groups'].items()}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['inspect', 'freeze', 'score'])
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(ROOT / 'var/experiments'):
        parser.error('Results must be inside isolated experiments')
    {'inspect': inspect, 'freeze': freeze, 'score': evaluate}[args.mode](root)


if __name__ == '__main__':
    main()
