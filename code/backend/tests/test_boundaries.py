import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.client import APIError, Client
from app.config import Settings
from app.pipeline import validate_detection, validate_events, validate_verification
from app.policy import guard_identity, possible_duplicates
from app.schemas import Detection, Events, Verification, View, pixel_box
from app.tools import ToolBox


@pytest.mark.parametrize('box', [[0, 0, 1001, 1000], [1, 1, 0, 2], [0, 0, float('nan'), 5], [0, 0, 0, 0]])
def test_invalid_box_rejected(box):
    with pytest.raises(ValidationError):
        View(frame_id='f1', bbox=box)


def test_original_resolution_crop_and_clamp():
    assert pixel_box([0, 0, 1000, 1000], 1920, 1080) == (0, 0, 1920, 1080)
    assert pixel_box([250, 250, 750, 750], 1920, 1080, padding=0) == (480, 270, 1440, 810)


def test_unknown_frame_cannot_be_used():
    value = Detection.model_validate({'objects': [{'candidate_id': 'a', 'name': 'item',
        'category': 'unknown', 'description': '', 'views': [{'frame_id': 'fabricated', 'bbox': [0, 0, 100, 100]}]}], 'uncertainties': []})
    with pytest.raises(ValueError, match='Unknown frame'):
        validate_detection(value, {'f1'})


def test_readonly_tools_and_external_search_off(monkeypatch):
    monkeypatch.setenv('MIND_BOX_WEB_SEARCH', 'false')
    toolbox = ToolBox({'crop1': {'id': 'crop1'}})
    assert toolbox.call('run_shell', {'command': 'anything'})['status'] == 'rejected'
    assert toolbox.call('get_evidence', {'evidence_id': '../../.env'})['status'] == 'rejected'
    assert toolbox.call('get_evidence', {'evidence_id': 'crop1'})['status'] == 'ok'
    assert toolbox.call('search_catalog', {'query': '饮品', 'limit': '5'})['status'] == 'rejected'
    assert toolbox.call('search_product_images', {'query': 'drink'})['status'] == 'unavailable'
    results = toolbox.call('search_catalog', {'query': '饮品 茶饮 瓶装饮料', 'limit': 999})['results']
    assert results and len(results) <= 5
    assert all(r['status'] == 'category_definition' for r in results)


def test_brand_must_appear_in_transcribed_text():
    item = {'name': 'ImaginedBrand food', 'category': '食品', 'brand': 'ImaginedBrand', 'variant': None,
            'readable_text': ['food'], 'category_support': 'high', 'identity_support': 'high'}
    guarded = guard_identity(item)
    assert guarded['brand'] is None and guarded['identity_support'] == 'low'
    assert 'ImaginedBrand' not in guarded['name']
    assert guarded['identity_policy']['unsupported_candidates']['brand'] == 'ImaginedBrand'


def test_overlap_flags_do_not_merge_inventory():
    items = [{'candidate_id': 'a', 'category': '食品', 'evidence_refs': ['x']},
             {'candidate_id': 'b', 'category': '食品', 'evidence_refs': ['y']}]
    evidence = {e: {'frame_id': 'f1', 'bbox_normalized_xyxy': [0, 0, 500, 500]} for e in ('x', 'y')}
    assert len(possible_duplicates(items, evidence)) == 1
    assert len(items) == 2 and all(i['needs_review'] for i in items)


@pytest.mark.parametrize('statuses,expected_calls,success', [([429, 200], 2, True), ([400], 1, False), ([503, 503, 503], 3, False)])
def test_bounded_retry_and_secret_redaction(tmp_path, monkeypatch, statuses, expected_calls, success):
    secret = 'temporary-test-secret'
    calls = []

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            status = statuses[len(calls)]
            calls.append(status)
            request = httpx.Request('POST', url)
            if status == 200:
                return httpx.Response(status, json={'model': 'test', 'choices': [{'message': {'content': 'OK'}}]}, request=request)
            return httpx.Response(status, text='failure ' + secret, request=request)

    monkeypatch.setattr(httpx, 'Client', FakeHTTP)
    client = Client(Settings('https://example.invalid', secret, 'test', attempts=3), tmp_path, sleeper=lambda _: None)
    if success:
        assert client.chat([{'role': 'user', 'content': 'hello'}], 'test')['model'] == 'test'
        client.chat([{'role': 'user', 'content': 'hello'}], 'test')
    else:
        with pytest.raises(APIError):
            client.chat([{'role': 'user', 'content': 'hello'}], 'test')
    assert len(calls) == expected_calls
    log = (tmp_path / 'api-attempts.jsonl').read_text(encoding='utf-8')
    assert secret not in log and '[REDACTED]' in log
    if success:
        assert json.loads(log.splitlines()[-1])['status'] == 'cache_hit'


def test_cross_candidate_crop_reference_is_rejected():
    item = {'candidate_id': 'a', 'target_kind': 'movable_item', 'name': 'food', 'category': '食品',
            'description': '', 'brand': None, 'variant': None, 'readable_text': [],
            'category_support': 'low', 'identity_support': 'low', 'support_reason': '',
            'localization_status': 'usable', 'evidence_refs': ['crop-b']}
    value = Verification.model_validate({'items': [item]})
    evidence = {'crop-a': {'candidate_id': 'a'}, 'crop-b': {'candidate_id': 'b'}}
    with pytest.raises(ValueError):
        validate_verification(value, evidence)


@pytest.mark.parametrize('before,after,ids', [('late', 'early', ['a']), ('missing', 'late', ['a']), ('early', 'late', ['forged'])])
def test_event_cannot_invent_evidence_or_reverse_time(before, after, ids):
    value = Events.model_validate({'events': [{'kind': 'add', 'candidate_ids': ids,
        'before_frame_id': before, 'after_frame_id': after, 'explanation': '', 'support': 'low'}], 'uncertainties': []})
    frames = {'early': {'media_time_ms': 100}, 'late': {'media_time_ms': 200}}
    with pytest.raises(ValueError):
        validate_events(value, frames, {'a'})
