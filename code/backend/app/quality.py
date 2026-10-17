"""Separate readability, object association and action evidence."""
from .media import visual_quality


def assess_quality(item, evidence):
    views = []
    for ref in item.get('evidence_refs', []):
        entry = evidence.get(ref, {})
        metrics = entry.get('crop_quality', {})
        if 'sharpness' not in metrics:
            continue
        width, height = entry.get('crop_size', [256, 144])
        report = visual_quality(metrics['sharpness'], metrics.get('bad_exposure', 0), width, height,
            visibility_fraction=item.get('visibility_fraction'),
            occlusion_level=item.get('occlusion_level', 'unknown'),
            edge_contacts=metrics.get('image_edge_contacts', 0))
        views.append({'evidence_id': ref, **report})
    reasons = []
    if item.get('target_kind') != 'movable_item':
        reasons.append('movable_object_not_supported')
    if item.get('localization_status') != 'usable':
        reasons.append('unique_object_crop_not_usable')
    if item.get('object_support') == 'low':
        reasons.append('physical_object_not_recognizable')
    warnings = sorted({warning for view in views for warning in view['warnings']})
    if any(view['tier'] != 'clear' for view in views):
        warnings.append('degraded_image_allow_coarse_description')
    if item.get('occlusion_level') in ('moderate', 'severe'):
        warnings.append('estimated_occlusion_requires_temporal_association')
    return {'status': 'pending' if reasons else 'eligible_with_warning' if warnings else 'eligible',
            'reasons': reasons, 'warnings': sorted(set(warnings)), 'views': views,
            'action_check': 'required_separately', 'probability': None,
            'policy': 'evidence-quality-v1',
            'note': 'Pixel metrics do not veto a recognizable physical item; SKU/OCR support is independent.'}
