"""Read original PTS, scan at bounded frequency, select quality/diversity frames."""
from pathlib import Path
import math
import hashlib
import wave

import av
import cv2
import numpy as np
from PIL import Image, ImageDraw

from .client import write_json

EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.webm'}


def describe_container(container):
    streams = []
    for stream in container.streams:
        ctx = stream.codec_context
        info = {'index': stream.index, 'type': stream.type,
                'codec': ctx.name if ctx else None, 'time_base': str(stream.time_base),
                'start_pts': stream.start_time, 'duration_pts': stream.duration}
        if stream.type == 'video':
            info.update(width=stream.width, height=stream.height,
                        average_rate=str(stream.average_rate),
                        pixel_format=ctx.format.name if ctx.format else None,
                        rotation_metadata=stream.metadata.get('rotate'))
        elif stream.type == 'audio':
            info.update(sample_rate=ctx.sample_rate, layout=ctx.layout.name,
                        channels=len(ctx.layout.channels))
        streams.append(info)
    return {'format': container.format.name, 'start_time_us': container.start_time,
            'duration_us': container.duration, 'bit_rate': container.bit_rate, 'streams': streams}


def inspect_media(path):
    path = Path(path)
    with av.open(str(path)) as container:
        return {'filename': path.name, 'bytes': path.stat().st_size,
                'container': describe_container(container)}


def video_files(path):
    path = Path(path)
    if path.is_file():
        return [path] if path.suffix.lower() in EXTENSIONS else []
    return sorted(p for p in path.iterdir() if p.suffix.lower() in EXTENSIONS and p.is_file())


def signature(image):
    small = cv2.resize(image, (256, 144))
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bad_exposure = float(np.mean((gray < 12) | (gray > 245)))
    thumb = cv2.resize(gray, (32, 18)).astype(np.float32)
    return sharpness, bad_exposure, thumb


def visual_quality(sharpness, bad_exposure, width, height, *, visibility_fraction=None,
                   occlusion_level='unknown', edge_contacts=0):
    """Diagnostic bands at signature's 256x144 scale, never a storage veto.

    Texture affects Laplacian variance; visibility is a model estimate, not a mask area.
    """
    if width < 32 or height < 32:
        blur = 'severe'
    elif sharpness < 35:
        blur = 'severe'
    elif sharpness < 120:
        blur = 'soft'
    else:
        blur = 'clear'
    exposure = 'severe' if bad_exposure > 0.55 else 'elevated' if bad_exposure > 0.30 else 'normal'
    visibility = 'unknown'
    if visibility_fraction is not None:
        visibility = ('severe' if visibility_fraction < 0.45 else
                      'partial' if visibility_fraction < 0.70 else 'mostly_visible')
    occlusion = occlusion_level if occlusion_level in ('none', 'mild', 'moderate', 'severe') else 'unknown'
    warnings = []
    if blur == 'severe':
        warnings.append('severe_blur')
    if exposure == 'severe':
        warnings.append('severe_exposure')
    if visibility == 'severe':
        warnings.append('low_visible_fraction')
    if occlusion == 'severe':
        warnings.append('severe_occlusion')
    if edge_contacts >= 2:
        warnings.append('crop_touches_multiple_image_edges')
    tier = 'poor' if warnings else 'soft' if blur == 'soft' or exposure == 'elevated' or visibility == 'partial' or occlusion == 'moderate' else 'clear'
    return {'tier': tier, 'blur': blur, 'exposure': exposure, 'visibility': visibility,
            'occlusion': occlusion, 'sharpness': round(float(sharpness), 2),
            'bad_exposure': round(float(bad_exposure), 4), 'width': int(width), 'height': int(height),
            'edge_contacts': int(edge_contacts), 'warnings': warnings,
            'visibility_fraction_estimate': visibility_fraction,
            'threshold_status': 'provisional_uncalibrated',
            'policy': 'diagnostic_only_identity_and_action_evidence_decide_storage'}


def action_frames(path, output, fps=8, start_ms=0, end_ms=None, max_frames=480):
    """Keep temporal coverage and the real last frame; never silently truncate."""
    path, output = Path(path), Path(output)
    destination = output / 'action-frames'
    destination.mkdir(parents=True, exist_ok=True)
    if not 1 <= fps <= 30 or start_ms < 0 or (end_ms is not None and end_ms < start_ms):
        raise ValueError('Invalid action frame range or fps')
    frames, last, next_ms = [], None, float(start_ms)

    def save(frame, time_ms, rotation):
        image = frame.to_image()
        if rotation:
            image = image.rotate(-rotation, expand=True)
        image.thumbnail((1280, 1280))
        frame_id = f'{path.stem}-t{frame.pts}'
        filename = f'action-frames/{frame_id}.jpg'
        image.save(output / filename, quality=92)
        sharpness, exposure, _ = signature(np.asarray(image))
        return {'id': frame_id, 'file': filename, 'model_file': filename,
                'pts': frame.pts, 'media_time_ms': time_ms,
                'width': image.width, 'height': image.height,
                'quality': round(math.log1p(sharpness) - 3 * exposure, 4)}

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = (container.start_time or 0) / av.time_base
        rotation = int(stream.metadata.get('rotate', '0')) % 360
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            t = round((float(frame.pts * frame.time_base) - origin) * 1000)
            if t < start_ms:
                continue
            if end_ms is not None and t > end_ms:
                break
            last = (frame, t)
            if t < next_ms:
                continue
            if len(frames) >= max_frames:
                raise ValueError('Action frame budget exceeded; split the recording into operations')
            frames.append(save(frame, t, rotation))
            next_ms = t + 1000 / fps
        if last and (not frames or last[0].pts != frames[-1]['pts']):
            if len(frames) >= max_frames:
                raise ValueError('Action frame budget exceeded at final frame')
            frames.append(save(last[0], last[1], rotation))
    if not frames:
        raise ValueError('No timestamped frames in action range')
    return frames


def iter_action_frames(path, output, fps=12):
    """Yield one delivered frame at a time, preserving the final frame and original PTS."""
    path, output = Path(path), Path(output)
    (output / 'action-frames').mkdir(parents=True, exist_ok=True)
    if not 1 <= fps <= 30:
        raise ValueError('Invalid causal sampling fps')
    next_ms, last, emitted_pts = 0, None, None
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = (container.start_time or 0) / av.time_base
        rotation = int(stream.metadata.get('rotate', '0')) % 360

        def save(frame, ms):
            image = frame.to_image()
            if rotation:
                image = image.rotate(-rotation, expand=True)
            image.thumbnail((1280, 1280))
            identity = f'{path.stem}-t{frame.pts}'
            filename = f'action-frames/{identity}.jpg'
            image.save(output / filename, quality=94)
            sharpness, exposure, _ = signature(np.asarray(image))
            return {'id': identity, 'file': filename, 'pts': frame.pts, 'media_time_ms': ms,
                    'width': image.width, 'height': image.height,
                    'sharpness': sharpness, 'bad_exposure': exposure}

        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            ms = round((float(frame.pts * frame.time_base) - origin) * 1000)
            if ms < 0:
                continue
            last = (frame, ms)
            if ms < next_ms:
                continue
            emitted_pts = frame.pts
            yield save(frame, ms)
            next_ms = ms + 1000 / fps
        if last and last[0].pts != emitted_pts:
            yield save(*last)


def prepare_video(path, output, max_frames=8, scan_fps=3, extract_audio=True):
    path, output = Path(path), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    frames_dir = output / 'frames'
    frames_dir.mkdir(exist_ok=True)
    sha = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            sha.update(chunk)
    candidates = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        # One common origin, including audio offset, not separate per-stream zeroing.
        origin = float(container.start_time / av.time_base) if container.start_time is not None else 0.0
        meta = {'filename': path.name, 'bytes': path.stat().st_size, 'input_sha256': sha.hexdigest(),
                'selection_version': 2, 'requested_max_frames': max_frames, 'width': stream.width,
                'height': stream.height, 'fps': float(stream.average_rate or 0),
                'duration_s': float(container.duration / av.time_base) if container.duration else None,
                'origin_pts_s': origin, 'audio_streams': len(container.streams.audio),
                'time_base': str(stream.time_base), 'scan_fps': scan_fps,
                'container': describe_container(container)}
        next_t, previous = -math.inf, None
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            t = float(frame.pts * frame.time_base) - origin
            if t + 1e-5 < next_t:
                continue
            image = frame.to_ndarray(format='rgb24')
            # Honor display rotation when present in stream metadata.
            rotation = int(stream.metadata.get('rotate', '0')) % 360
            if rotation in (90, 180, 270):
                image = np.rot90(image, -(rotation // 90)).copy()
            sharpness, exposure, thumb = signature(image)
            change = float(np.mean(np.abs(thumb - previous))) if previous is not None else 255.0
            # Save scan proxies so memory stays bounded on longer recordings.
            scan_path = frames_dir / f'scan-{len(candidates):04d}.jpg'
            im = Image.fromarray(image)
            im.thumbnail((1280, 1280))
            im.save(scan_path, quality=88)
            candidates.append({'pts': frame.pts, 'pts_s': float(frame.pts * frame.time_base),
                               'media_time_ms': round(t * 1000), 'sharpness': round(sharpness, 2),
                               'bad_exposure': round(exposure, 4), 'change': round(change, 2),
                               'scan_path': str(scan_path.name),
                               'quality': math.log1p(sharpness) - 3 * exposure})
            previous = thumb
            next_t = t + 1 / scan_fps
    if not candidates:
        raise ValueError('No timestamped video frames')
    # Equal-time coverage; quality breaks ties within each temporal bucket.
    end = max(c['media_time_ms'] for c in candidates) + 1
    chosen = []
    for i in range(max_frames):
        bucket = [c for c in candidates if i <= c['media_time_ms'] / end * max_frames < i + 1]
        if bucket:
            chosen.append(max(bucket, key=lambda c: c['quality'] + min(c['change'], 30) / 30))
    if max_frames >= 2 and len(chosen) >= 2:
        chosen[0], chosen[-1] = candidates[0], candidates[-1]
    # Suppress only extremely similar representatives, preserving endpoint evidence.
    diverse, duplicate_frames = [], []
    previous_thumb = None
    for c in chosen:
        image = np.asarray(Image.open(frames_dir / c['scan_path']))
        _, _, thumb = signature(image)
        anchor = c is chosen[0] or c is chosen[-1]
        if previous_thumb is not None and not anchor and float(np.mean(np.abs(thumb - previous_thumb))) < 0.8:
            duplicate_frames.append(c['pts'])
            continue
        diverse.append(c)
        previous_thumb = thumb
    chosen = diverse
    selected = {c['pts']: c for c in chosen}
    selected_pts = set(selected)
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            if frame.pts not in selected_pts:
                continue
            c = selected[frame.pts]
            im = frame.to_image()
            rotation = int(stream.metadata.get('rotate', '0')) % 360
            if rotation:
                im = im.rotate(-rotation, expand=True)
            c['id'] = f'{path.stem}-f{len([v for v in selected.values() if "id" in v]):03d}'
            c['file'] = f'frames/{c["id"]}.jpg'
            c['width'], c['height'] = im.size
            im.save(output / c['file'], quality=95)
            proxy = im.copy()
            proxy.thumbnail((1280, 1280))
            c['model_file'] = f'frames/{c["id"]}-model.jpg'
            proxy.save(output / c['model_file'], quality=90)
    frames = sorted(selected.values(), key=lambda c: c['media_time_ms'])
    meta['candidate_count'] = len(candidates)
    meta['duplicate_representatives_suppressed'] = duplicate_frames
    meta['near_static_scan_pairs'] = sum(c['change'] < 2.0 for c in candidates[1:])
    meta['frames'] = frames
    meta['scan'] = candidates
    if extract_audio and meta['audio_streams']:
        audio_path = output / 'audio.wav'
        with av.open(str(path)) as container, wave.open(str(audio_path), 'wb') as wav:
            audio = container.streams.audio[0]
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
            first_pts = None
            for af in container.decode(audio):
                if first_pts is None and af.pts is not None:
                    first_pts = float(af.pts * af.time_base)
                for sample in resampler.resample(af):
                    wav.writeframes(sample.to_ndarray().tobytes())
            for sample in resampler.resample(None):
                wav.writeframes(sample.to_ndarray().tobytes())
            meta['audio'] = {'file': 'audio.wav', 'sample_rate': 16000,
                             'first_pts_s': first_pts, 'origin_pts_s': origin,
                             'first_media_time_ms': round((first_pts - origin) * 1000) if first_pts is not None else None,
                             'asr_status': 'not_run', 'note': 'WAV is for inspection; original audio/video remains authoritative for discontinuities.'}
    sheet(frames, output, output / 'contact-sheet.jpg')
    write_json(output / 'media.json', meta)
    return meta


def sheet(frames, root, destination):
    width, height, cols = 360, 260, 4
    canvas = Image.new('RGB', (width * cols, height * math.ceil(len(frames) / cols)), '#202020')
    draw = ImageDraw.Draw(canvas)
    for i, c in enumerate(frames):
        im = Image.open(Path(root) / c['file'])
        im.thumbnail((width, height - 30))
        x, y = (i % cols) * width, (i // cols) * height
        canvas.paste(im, (x, y))
        draw.text((x + 6, y + height - 26), f'{c["id"]}  {c["media_time_ms"] / 1000:.2f}s', fill='white')
    canvas.save(destination, quality=90)
