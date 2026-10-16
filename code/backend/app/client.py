"""Sequential OpenAI-compatible client; logs never contain request bodies/headers."""
import base64
import hashlib
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import Settings


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def image_part(path):
    return {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' +
            base64.b64encode(Path(path).read_bytes()).decode('ascii')}}


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    return json.loads(text)


class APIError(RuntimeError):
    pass


class Client:
    def __init__(self, settings: Settings, output: Path, sleeper=time.sleep):
        self.settings, self.output, self.sleeper = settings, Path(output), sleeper
        self.output.mkdir(parents=True, exist_ok=True)
        self.cache = self.output / 'cache'
        self.cache.mkdir(exist_ok=True)

    def safe(self, text):
        text = str(text)
        if self.settings.api_key:
            text = text.replace(self.settings.api_key, '[REDACTED]')
        text = re.sub(r'data:((?:image|video|audio)/[^;,]+);base64,[A-Za-z0-9+/=]+',
                      lambda match: '[' + match[1].split('/')[0].upper() + ']', text)
        return text[:800]

    def log(self, event):
        event['at'] = datetime.now(timezone.utc).isoformat()
        with (self.output / 'api-attempts.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    def chat(self, messages, stage, *, model=None, max_tokens=3000, json_mode=False,
             tools=None, tool_choice=None, use_cache=True):
        s = self.settings
        if not s.api_key:
            raise APIError('MIND_BOX_API_KEY is missing')
        payload = {'model': model or s.model, 'messages': messages,
                   'max_tokens': max_tokens, 'stream': False}
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = tool_choice or 'auto'
        key = hashlib.sha256((s.api_url + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        cache_path = self.cache / (key + '.json')
        if use_cache and cache_path.exists():
            self.log({'stage': stage, 'request_id': key[:16], 'status': 'cache_hit', 'model': payload['model']})
            return json.loads(cache_path.read_text(encoding='utf-8'))
        for attempt in range(1, s.attempts + 1):
            start = time.monotonic()
            event = {'stage': stage, 'request_id': key[:16], 'attempt': attempt, 'model': payload['model']}
            retry_after = 0
            try:
                with httpx.Client(verify=s.verify_tls, timeout=s.timeout) as http:
                    response = http.post(s.api_url, headers={'Authorization': 'Bearer ' + s.api_key}, json=payload)
                event['http_status'] = response.status_code
                if response.status_code >= 400:
                    event['error'] = self.safe(response.text)
                    retryable = response.status_code in (408, 429) or response.status_code >= 500
                    try:
                        retry_after = min(30, max(0, float(response.headers.get('retry-after', '0'))))
                    except ValueError:
                        pass
                    raise APIError('HTTP ' + str(response.status_code) + ': ' + event['error'])
                result = response.json()
                if not result.get('choices'):
                    retryable = False
                    raise APIError('Response has no choices: ' + self.safe(response.text))
                msg = result['choices'][0].get('message', {})
                if not msg.get('content') and not msg.get('tool_calls'):
                    retryable = True
                    raise APIError('Empty assistant content')
                event.update(status='success', actual_model=result.get('model'), usage=result.get('usage'),
                             finish_reason=result['choices'][0].get('finish_reason'))
                write_json(cache_path, result)
                return result
            except (httpx.TransportError, APIError, ValueError) as exc:
                if isinstance(exc, httpx.TransportError):
                    retryable = True
                elif isinstance(exc, ValueError):
                    retryable = True
                event.update(status='failed', error=self.safe(exc), error_type=type(exc).__name__)
                if attempt >= s.attempts or not retryable:
                    raise APIError(self.safe(exc)) from None
            finally:
                event['elapsed_s'] = round(time.monotonic() - start, 3)
                self.log(event)
            self.sleeper(max(retry_after, min(16, 2 ** attempt)) + random.random())
        raise APIError('Retry limit exhausted')


def content(result):
    return result['choices'][0]['message'].get('content') or ''
