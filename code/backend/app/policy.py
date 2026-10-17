"""Program rules reduce unsupported specificity; they cannot prove OCR correctness."""
import re
import unicodedata


def compact(value):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', value or '')).lower()


def descriptive_identity(item):
    """Use observed words, without guessing contents of an unfamiliar package."""
    description = (item.get('description') or '').strip()
    name = (item.get('name') or '').strip()
    generic = {'', '未知', '其他', '未知物品', '物品', 'unknown', 'object', 'item'}
    if name.lower() in generic:
        name = re.split(r'[。；;\n]', description)[0].strip(' ，,')[:100] or '待辨认物品'
    return {'name': name[:120], 'name_kind': 'attribute_description',
            'category': item.get('category') or '未知',
            'description': description[:1000] or name[:120]}


def guard_identity(item):
    item = dict(item)
    item['model_reported_support'] = {'category': item['category_support'], 'identity': item['identity_support']}
    readable = compact(' '.join(item['readable_text']))
    rejected = {}
    for field in ('brand', 'variant'):
        value = item.get(field)
        if value and compact(value) not in readable:
            rejected[field] = value
            item[field] = None
            item['name'] = item['name'].replace(value, '').strip() or item['category']
    if rejected:
        item['identity_support'] = 'low'
    elif item['identity_support'] == 'high':
        # These are model transcriptions, not independent OCR/user confirmation.
        item['identity_support'] = 'medium'
    item['identity_policy'] = {'unsupported_candidates': rejected,
                               'status': 'model_transcription_unverified',
                               'probability': None}
    return item


def box_iou(a, b):
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return area / max(1e-8, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - area)


def possible_duplicates(items, evidence):
    """Flag same-frame overlapping candidates; never infer physical quantity."""
    links = []
    for index, a in enumerate(items):
        for b in items[index + 1:]:
            if a['category'] != b['category']:
                continue
            overlap = max((box_iou(evidence[x]['bbox_normalized_xyxy'], evidence[y]['bbox_normalized_xyxy'])
                           for x in a['evidence_refs'] for y in b['evidence_refs']
                           if evidence[x]['frame_id'] == evidence[y]['frame_id']), default=0)
            if overlap > 0.60:
                a['needs_review'] = b['needs_review'] = True
                links.append({'candidate_ids': [a['candidate_id'], b['candidate_id']],
                              'reason': 'same_frame_box_overlap', 'max_iou': round(overlap, 3)})
    return links
