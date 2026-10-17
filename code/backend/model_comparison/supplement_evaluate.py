"""Post-inference four-arm scoring; original experiment remains read-only."""
import argparse
import json
from pathlib import Path
from statistics import median

from app.client import write_json
from app.config import ROOT
from model_comparison.deepseek_dense import SOURCE, verify
from model_comparison.evaluate import f1, load, raw_objects, score_case
from model_comparison.run import digest

GROUP = 'deepseek_dense'
GROUPS = ['deepseek_pipeline', GROUP, 'gpt_pipeline', 'gpt_dense']
NAMES = {'deepseek_pipeline': 'DeepSeek 原流程', GROUP: 'DeepSeek 密集帧整段',
         'gpt_pipeline': 'GPT 原流程', 'gpt_dense': 'GPT 密集帧整段'}


def freeze(root):
    target = root / 'manual-review.json'
    if target.exists():
        raise ValueError('Manual review already frozen')
    reviews = load(root / 'review-mappings.json')
    for i in range(1, 11):
        name = f'v{i}'
        path = root / GROUP / name / 'result.json'
        objects = raw_objects(load(path), GROUP)
        if set(reviews[name]['mapping']) != {o['object_id'] for o in objects}:
            raise ValueError('Review must cover every object: ' + name)
        reviews[name]['result_sha256'] = digest(path)
    write_json(target, reviews)
    write_json(root / 'experiment-code-hashes.json', {p.name: digest(p) for p in Path(__file__).parent.glob('*.py')})


def evaluate(root):
    baseline = load(SOURCE / 'comparison.json')
    labels = load(ROOT.parent / 'datasets/action-regression.json')['cases']
    reviews = load(root / 'manual-review.json')
    cases, durations = [], []
    for i in range(1, 11):
        name = f'v{i}'
        path = root / GROUP / name / 'result.json'
        if digest(path) != reviews[name]['result_sha256']:
            raise ValueError('Prediction changed after annotation: ' + name)
        result = load(path)
        cases.append(score_case(result, labels[name], reviews[name], GROUP))
        durations.append(result['elapsed_s'])
    logs = [json.loads(line) for line in (root / GROUP / 'api/api-attempts.jsonl').read_text(encoding='utf-8').splitlines()]
    success = [r for r in logs if r['status'] == 'success']
    summary = {key: sum(c[key] for c in cases) for key in (
        'instances_exact', 'recognition_exact', 'events_exact', 'net_exact', 'complete_correct',
        'usable_correct', 'unsafe_wrong', 'abstained')}
    summary.update(videos=len(cases), http_success=len(success),
        http_failed=sum(r['status'] == 'failed' for r in logs),
        invalid_outputs=sum(r['status'] == 'invalid_output' for r in logs),
        tokens=sum((r.get('usage') or {}).get('total_tokens', 0) for r in success),
        input_tokens=sum((r.get('usage') or {}).get('prompt_tokens', 0) for r in success),
        output_tokens=sum((r.get('usage') or {}).get('completion_tokens', 0) for r in success),
        http_elapsed_s=round(sum(r['elapsed_s'] for r in logs if r['status'] in ('success', 'failed')), 2),
        actual_models=sorted({r['actual_model'] for r in success if r.get('actual_model')}),
        median_video_elapsed_s=round(median(durations), 2), total_video_elapsed_s=round(sum(durations), 2),
        event_micro_f1=f1(sum(c['event_tp'] for c in cases), sum(c['event_predictions'] for c in cases), sum(c['event_expected'] for c in cases)),
        net_micro_f1=f1(sum(c['net_tp'] for c in cases), sum(c['net_predictions'] for c in cases), sum(c['net_expected'] for c in cases)))
    groups = {**baseline['groups'], GROUP: {'summary': summary, 'cases': cases}}
    report = {'scope': '10 development videos, single run per arm; fourth-arm supplement.',
              'baseline_comparison_sha256': digest(SOURCE / 'comparison.json'),
              'groups': {g: groups[g] for g in GROUPS}, 'isolation': verify(root),
              'ground_truth_sha256': digest(ROOT.parent / 'datasets/action-regression.json'),
              'review_sha256': digest(root / 'manual-review.json')}
    write_json(root / 'comparison.json', report)
    lines = ['# DeepSeek 密集帧整段判断：第四组补测', '',
        '日期：2026-10-17。同一批 v1-v10 开发视频，每组一轮；视觉任务，不含语音、日期、日程与库存提交。', '',
        '## 汇总', '', '| 指标 | ' + ' | '.join(NAMES[g] for g in GROUPS) + ' |',
        '| --- | --- | --- | --- | --- |']
    for label, key in [('基础认物完整正确', 'recognition_exact'), ('完整动作正确（v7 含顺序）', 'events_exact'),
                       ('最终净变化正确', 'net_exact'), ('三项同时正确', 'complete_correct'),
                       ('正确且动作/直判无阻塞', 'usable_correct')]:
        lines.append('| ' + label + ' | ' + ' | '.join(f'{groups[g]["summary"][key]}/10' for g in GROUPS) + ' |')
    for label, key in [('HTTP 成功调用', 'http_success'), ('HTTP 失败尝试', 'http_failed'),
                       ('结构/引用校验拒绝', 'invalid_outputs'), ('成功请求回报 tokens', 'tokens'),
                       ('单视频耗时中位数（秒）', 'median_video_elapsed_s'),
                       ('动作 micro F1', 'event_micro_f1'), ('净变化 micro F1', 'net_micro_f1')]:
        lines.append('| ' + label + ' | ' + ' | '.join(str(groups[g]['summary'][key]) for g in GROUPS) + ' |')
    lines += ['', '## 逐视频', '', '每格为基础认物 / 动作 / 净变化，1=该项完整正确，0=有错漏。', '',
              '| 视频 | ' + ' | '.join(NAMES[g] for g in GROUPS) + ' |', '| --- | --- | --- | --- | --- |']
    for i in range(10):
        values = [' / '.join(str(int(groups[g]['cases'][i][key])) for key in
                            ('recognition_exact', 'events_exact', 'net_exact')) for g in GROUPS]
        lines.append(f'| v{i + 1} | ' + ' | '.join(values) + ' |')
    lines += ['', '## DeepSeek 密集组人工复核', '']
    for i, case in enumerate(cases, 1):
        lines.append(f'- v{i}：' + '；'.join(case['review_notes']))
    lines += ['', '## 可比性与边界', '',
        '- 新组直接复用上一轮 GPT 密集组的全部 229 张 JPEG（每段 13-40 张），帧 ID、媒体时间、图片字节、提示词与 JSON schema 相同；4fps 目标采样、覆盖整段并保留末帧。没有重新取帧。',
        '- 原流程已有 8fps 局部动作取帧；整段直判改变了上下文组织与处理流程，不能把效果只归因于“帧更多”。',
        '- DeepSeek 使用现有 deepseek-pro 网关、Chat Completions、max_tokens=8000、至多一次结构修复；GPT 使用 Responses、low reasoning、high image detail 与额外推理 token 预算。图片预处理与协议条件并非全部相同。',
        '- 推理不导入人工答案或历史模型输出；推理后按实际新 ID 人工复核，结果哈希冻结于 manual-review.json。基础认物接受正确食品/饮品大类，不要求品牌、口味或容量；明确错类或未确认大类仍扣分。',
        '- 延续原评分：逐件对应、动作、净变化独立判断；重复实物不能合并成正确；v7 要求完整顺序。人工答案未穷尽内部挪动，所有组统一排除 move，但保留其原始记录。',
        '- 直接预测正确不等于可直接写库；直判与原流程的人工确认机制不同。置信度与误写风险仍需单独验证。',
        '- 旧三组结果直接读取历史 comparison.json，保持原预测与标注不变。正常代码近期有独立改动，本次快照以补测开始时为准；实验不调用取帧模块，也不写正常配置或数据库。',
        '- 首次沙箱连接被系统拦截，未到达服务，保存在独立的 model-compare-20261017-deepseek-dense 目录；本表只汇总获得网络访问后的本轮。',
        '- 模型名称与 tokens 由网关回报，底层身份及价格未独立核验。10 段是既有开发视频，单轮结果只能说明此批表现，不能当作泛化准确率。',
        '- 隔离校验：' + json.dumps(report['isolation'], ensure_ascii=False) + '。', '',
        '证据：manifest.json（完整输入与旧实验哈希）；deepseek_dense/v*/frames.json 与图片（相同输入）；deepseek_dense/v*/result.json（预测）；deepseek_dense/api/api-attempts.jsonl（请求与用量）；manual-review.json（人工对应）；comparison.json（四组评分）。', '']
    (root / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['freeze', 'score'])
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(ROOT / 'var/experiments') or root == SOURCE or root.is_relative_to(SOURCE):
        parser.error('Use a separate supplemental experiment directory')
    {'freeze': freeze, 'score': evaluate}[args.mode](root)


if __name__ == '__main__':
    main()
