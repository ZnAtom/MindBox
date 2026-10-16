"""Isolated MP4 capability probe; a successful HTTP response is not a video pass."""
import argparse
import base64
import hashlib
import json
import random
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import av
import httpx
from PIL import Image, ImageDraw

from .client import APIError, Client, content, image_part, parse_json, write_json
from .config import Settings


CONTROL_PROMPT = (
    'Observe the attached media in chronological order. Report each distinct phase of the '
    'large colored shape, merging consecutive identical frames. Do not guess unseen phases. '
    'Reply only JSON: {"phases":[{"color":"red|green|blue|yellow|magenta|cyan",'
    '"shape":"rectangle|circle|triangle"}],"media_readable":true|false}. '
    'If media is unavailable, use an empty phases array and media_readable=false.'
)
COLORS = {'red': '#ed2525', 'green': '#21b14b', 'blue': '#244fee',
          'yellow': '#f0db18', 'magenta': '#d72cdb', 'cyan': '#12ccd5'}
SHAPES = ('rectangle', 'circle', 'triangle')


def sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def video_part(path):
    path = Path(path)
    if path.suffix.lower() != '.mp4' or path.stat().st_size > 15 * 1024 * 1024:
        raise ValueError('Probe accepts MP4 files of at most 15 MiB')
    return {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' +
            base64.b64encode(path.read_bytes()).decode('ascii')}}


def make_controls(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.SystemRandom()
    colors = rng.sample(list(COLORS), 3)
    shapes = rng.sample(list(SHAPES), 3)
    phases = [dict(color=color, shape=shape) for color, shape in zip(colors, shapes)]
    frames = []
    for i, phase in enumerate(phases):
        im = Image.new('RGB', (320, 240), 'white')
        draw = ImageDraw.Draw(im)
        fill = COLORS[phase['color']]
        if phase['shape'] == 'rectangle':
            draw.rectangle((70, 65, 250, 175), fill=fill)
        elif phase['shape'] == 'circle':
            draw.ellipse((85, 45, 235, 195), fill=fill)
        else:
            draw.polygon(((160, 40), (65, 200), (255, 200)), fill=fill)
        im.save(output / f'control-image-{i}.jpg')
        frames.append(im)
    clips = []
    for i, order in enumerate((list(range(3)), list(reversed(range(3))))):
        path = output / f'control-{i}.mp4'
        with av.open(str(path), 'w') as container:
            stream = container.add_stream('libx264', rate=8)
            stream.width, stream.height, stream.pix_fmt = 320, 240, 'yuv420p'
            stream.options = {'crf': '22', 'preset': 'fast'}
            for frame_index in range(24):
                frame = av.VideoFrame.from_image(frames[order[frame_index // 8]])
                frame.pts, frame.time_base = frame_index, Fraction(1, 8)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        clips.append({'file': path.name, 'sha256': sha256(path), 'bytes': path.stat().st_size,
                      'duration_s': 3, 'expected': [phases[n] for n in order]})
    return clips


def temporal_check(text, expected):
    try:
        parsed = parse_json(text)
        phases = parsed.get('phases')
        return {'passed': parsed.get('media_readable') is True and phases == expected,
                'parsed': parsed}
    except (ValueError, TypeError, AttributeError) as exc:
        return {'passed': False, 'parse_error': str(exc)[:120]}


def response_url(chat_url):
    parts = urlsplit(chat_url)
    suffix = '/start/chat/completions'
    if not parts.path.endswith(suffix):
        raise ValueError('No known sibling Responses endpoint for this configured URL')
    return urlunsplit((parts.scheme, parts.netloc, parts.path[:-len(suffix)] + '/response', '', ''))


def responses_request(client, path, prompt, stage):
    """Bounded alternate-format hypothesis, not a claim of provider documentation."""
    s = client.settings
    endpoint = response_url(s.api_url)
    part = video_part(path)
    payload = {'model': s.model, 'input': [{'role': 'user', 'content': [
        {'type': 'input_text', 'text': prompt},
        {'type': 'input_video', 'video_url': part['video_url']['url']}]}],
        'max_output_tokens': 1200, 'stream': False}
    for attempt in range(1, s.attempts + 1):
        start = time.monotonic()
        event = {'stage': stage, 'attempt': attempt, 'model': s.model, 'protocol': 'responses_input_video'}
        retryable, retry_after = False, 0
        try:
            with httpx.Client(verify=s.verify_tls, timeout=s.timeout) as http:
                response = http.post(endpoint, headers={'Authorization': 'Bearer ' + s.api_key}, json=payload)
            event['http_status'] = response.status_code
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            if response.status_code >= 400:
                try:
                    retry_after = min(30, max(0, float(response.headers.get('retry-after', '0'))))
                except ValueError:
                    pass
                raise APIError('HTTP ' + str(response.status_code) + ': ' + client.safe(response.text))
            result = response.json()
            answer = result.get('output_text') or ''.join(
                part.get('text', '') for msg in result.get('output', [])
                for part in msg.get('content', []) if part.get('type') == 'output_text')
            if not answer:
                raise APIError('No Responses output_text: ' + client.safe(response.text))
            event.update(status='success', actual_model=result.get('model'), usage=result.get('usage'))
            return answer, result
        except (httpx.TransportError, APIError, ValueError) as exc:
            if isinstance(exc, httpx.TransportError):
                retryable = True
            event.update(status='failed', error=client.safe(exc), error_type=type(exc).__name__)
            if not retryable or attempt >= s.attempts:
                raise APIError(client.safe(exc)) from None
        finally:
            event['elapsed_s'] = round(time.monotonic() - start, 3)
            client.log(event)
        client.sleeper(max(retry_after, min(16, 2 ** attempt)))


def ask_video(client, path, prompt, protocol, stage):
    if protocol == 'responses_input_video':
        return responses_request(client, path, prompt, stage)
    result = client.chat([{'role': 'user', 'content': [
        {'type': 'text', 'text': prompt}, video_part(path)]}], stage,
        max_tokens=1200, use_cache=False)
    return content(result), result


def run_probe(output, protocol='chat_video_url', attempts=3):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    settings = replace(Settings.load(), attempts=attempts, timeout=90)
    client = Client(settings, output)
    clips = make_controls(output)
    endpoint = response_url(settings.api_url) if protocol == 'responses_input_video' else settings.api_url
    report = {'requested_model': settings.model, 'endpoint': endpoint,
              'prompt_sha256': hashlib.sha256(CONTROL_PROMPT.encode()).hexdigest(),
              'protocol': protocol, 'format_documented_by_provider': False,
              'tls_verified': settings.verify_tls, 'controls': clips,
              'native_video_status': 'not_tested', 'database_writes': 0, 'trials': []}
    write_json(output / 'report.json', report)
    for i, clip in enumerate(clips):
        trial = {'file': clip['file']}
        try:
            answer, raw = ask_video(client, output / clip['file'], CONTROL_PROMPT, protocol, f'video_control_{i}')
            trial.update(status='responded', answer=client.safe(answer), actual_model=raw.get('model'),
                         usage=raw.get('usage'), **temporal_check(answer, clip['expected']))
            write_json(output / f'response-{i}.json', raw)
        except APIError as exc:
            trial.update(status='failed', passed=False, error=client.safe(exc))
        report['trials'].append(trial)
        write_json(output / 'report.json', report)
        print(f"{protocol} control-{i}: {trial['status']}; passed={trial['passed']}", flush=True)
        if trial['status'] == 'failed':
            break
    report['native_video_status'] = ('temporal_controls_passed' if len(report['trials']) == 2 and
                                     all(t['passed'] for t in report['trials']) else 'not_verified')
    write_json(output / 'report.json', report)
    return report


def image_control(output, controls):
    output, controls = Path(output), Path(controls)
    output.mkdir(parents=True, exist_ok=False)
    client = Client(replace(Settings.load(), attempts=3, timeout=90), output)
    parts = [{'type': 'text', 'text': CONTROL_PROMPT}]
    parts.extend(image_part(controls / f'control-image-{i}.jpg') for i in range(3))
    expected = json.loads((controls / 'report.json').read_text(encoding='utf-8'))['controls'][0]['expected']
    try:
        response = client.chat([{'role': 'user', 'content': parts}], 'image_control', max_tokens=600, use_cache=False)
        answer = content(response)
        report = dict(status='responded', input_kind='three_images_not_video', answer=client.safe(answer),
                      actual_model=response.get('model'), usage=response.get('usage'),
                      **temporal_check(answer, expected))
        write_json(output / 'response.json', response)
    except APIError as exc:
        report = dict(status='failed', passed=False, error=client.safe(exc))
    write_json(output / 'report.json', report)
    print('image_control:', report['status'], 'passed=', report['passed'], flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--protocol', choices=['chat_video_url', 'responses_input_video'], default='chat_video_url')
    parser.add_argument('--image-controls', type=Path)
    args = parser.parse_args()
    if args.image_controls:
        image_control(args.output, args.image_controls)
    else:
        run_probe(args.output, args.protocol)


if __name__ == '__main__':
    main()
