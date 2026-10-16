"""Positive local-feature matches help distinguish packaging; no match is not absence."""
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


class AppearanceMatcher:
    def __init__(self, result, source):
        self.detector = cv2.SIFT_create(nfeatures=1400)
        self.matcher = cv2.BFMatcher()
        self.templates = []
        for item in result['items']:
            if item.get('localization_status') != 'usable':
                continue
            for ref in item.get('evidence_refs', [])[:3]:
                ev = result['evidence'][ref]
                image = cv2.imread(str(Path(source) / ev['file']), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    continue
                points, descriptors = self.detector.detectAndCompute(image, None)
                if descriptors is not None and len(points) >= 8:
                    self.templates.append((item['candidate_id'], image.shape, points, descriptors))

    def match(self, image):
        gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
        points, descriptors = self.detector.detectAndCompute(gray, None)
        if descriptors is None or len(points) < 8:
            return []
        found = {}
        for cid, shape, template_points, template_descriptors in self.templates:
            pairs = self.matcher.knnMatch(template_descriptors, descriptors, k=2)
            good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance]
            if len(good) < 8:
                continue
            a = np.float32([template_points[m.queryIdx].pt for m in good])
            b = np.float32([points[m.trainIdx].pt for m in good])
            matrix, mask = cv2.findHomography(a, b, cv2.RANSAC, 4)
            if matrix is None or mask is None:
                continue
            inliers = mask.ravel().astype(bool)
            count, ratio = int(inliers.sum()), float(inliers.mean())
            if count < 8 or ratio < 0.25:
                continue
            coverage = cv2.contourArea(cv2.convexHull(a[inliers])) / (shape[0] * shape[1])
            if coverage < 0.08:
                continue
            corners = np.float32([[[0, 0], [shape[1], 0], [shape[1], shape[0]], [0, shape[0]]]])
            quad = cv2.perspectiveTransform(corners, matrix)[0]
            if not np.isfinite(quad).all() or not cv2.isContourConvex(quad):
                continue
            area = abs(cv2.contourArea(quad))
            x1, y1 = np.min(quad, axis=0)
            x2, y2 = np.max(quad, axis=0)
            width, height = image.size
            if not 400 < area < width * height * 0.65 or not 16 < x2 - x1 < width or not 16 < y2 - y1 < height:
                continue
            if x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height:
                continue
            score = count * ratio * coverage
            hit = {'candidate_id': cid, 'bbox_pixels': [round(float(v)) for v in (x1, y1, x2, y2)],
                   'inliers': count, 'inlier_ratio': round(ratio, 3), 'template_coverage': round(coverage, 3),
                   'score': round(score, 3), 'meaning': 'positive geometric hint, not a calibrated identity probability'}
            if cid not in found or score > found[cid]['score']:
                found[cid] = hit
        return list(found.values())

    def annotate(self, frame, root):
        root = Path(root)
        with Image.open(root / frame['file']) as source:
            im = source.copy()
        hits = self.match(im)
        draw, font = ImageDraw.Draw(im), ImageFont.load_default(size=22)
        for hit in hits:
            box = hit['bbox_pixels']
            box = (max(0, box[0]), max(0, box[1]), min(im.width, box[2]), min(im.height, box[3]))
            draw.rectangle(box, outline='#10b540', width=3)
            draw.text((box[0], max(0, box[1] - 25)), hit['candidate_id'] + ' SIFT', fill='#10b540', font=font, stroke_width=1)
        directory = root / 'matched-frames'
        directory.mkdir(exist_ok=True)
        filename = directory / (frame['id'] + '.jpg')
        im.save(filename, quality=95)
        frame['matched_file'], frame['appearance_hints'] = str(filename.relative_to(root)), hits
        return hits
