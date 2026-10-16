import numpy as np
from PIL import Image

from app.matching import AppearanceMatcher


def test_textured_object_matches_translation_but_blank_image_is_not_an_absence_claim(tmp_path):
    random = np.random.default_rng(37)
    template = random.integers(0, 255, (160, 180, 3), dtype=np.uint8)
    Image.fromarray(template).save(tmp_path / 'template.png')
    result = {'items': [{'candidate_id': 'a', 'localization_status': 'usable', 'evidence_refs': ['e']}],
              'evidence': {'e': {'file': 'template.png'}}}
    matcher = AppearanceMatcher(result, tmp_path)
    scene = np.zeros((500, 600, 3), dtype=np.uint8)
    scene[150:310, 200:380] = template
    hits = matcher.match(Image.fromarray(scene))
    assert len(hits) == 1 and hits[0]['candidate_id'] == 'a'
    assert all(abs(a - b) < 3 for a, b in zip(hits[0]['bbox_pixels'], [200, 150, 380, 310]))
    assert matcher.match(Image.new('RGB', (600, 500))) == []


def test_ransac_with_no_inliers_is_a_miss_not_a_batch_crash(tmp_path, monkeypatch):
    random = np.random.default_rng(73)
    im = Image.fromarray(random.integers(0, 255, (180, 180, 3), dtype=np.uint8))
    im.save(tmp_path / 'template.png')
    result = {'items': [{'candidate_id': 'x', 'localization_status': 'usable', 'evidence_refs': ['e']}],
              'evidence': {'e': {'file': 'template.png'}}}
    matcher = AppearanceMatcher(result, tmp_path)
    monkeypatch.setattr('app.matching.cv2.findHomography', lambda a, b, *args:
                        (np.eye(3), np.zeros((len(a), 1), dtype=np.uint8)))
    assert matcher.match(im) == []
