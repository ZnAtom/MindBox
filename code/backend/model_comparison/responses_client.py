"""Responses transport adapting the existing pipeline's chat-shaped contract."""
import hashlib
import json
import time

import httpx

from app.client import APIError, Client, write_json


def response_input(messages):
    items = []
    for message in messages:
        role = message['role']
        if role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                          'output': message['content']})
            continue
        body = message.get('content')
        if body:
            if isinstance(body, list):
                parts = []
                for part in body:
                    if part['type'] == 'image_url':
                        parts.append({'type': 'input_image', 'image_url': part['image_url']['url'],
                                      'detail': 'high'})
                    else:
                        parts.append({'type': 'output_text' if role == 'assistant' else 'input_text',
                                      'text': part['text']})
                body = parts
            items.append({'role': role, 'content': body})
        for call in message.get('tool_calls') or []:
            items.append({'type': 'function_call', 'call_id': call['id'],
                          'name': call['function']['name'], 'arguments': call['function']['arguments']})
    return items


class ResponsesClient(Client):
    def chat(self, messages, stage, *, model=None, max_tokens=3000, json_mode=False,
             tools=None, tool_choice=None, use_cache=True):
        payload = {'model': model or self.settings.model, 'input': response_input(messages),
                   'store': False, 'stream': False, 'max_output_tokens': max_tokens + 4000,
                   'reasoning': {'effort': 'low'}}
        if json_mode:
            payload['text'] = {'format': {'type': 'json_object'}}
        if tools:
            payload['tools'] = [{'type': 'function', **tool['function'], 'strict': False} for tool in tools]
            if isinstance(tool_choice, dict):
                payload['tool_choice'] = {'type': 'function', 'name': tool_choice['function']['name']}
            else:
                payload['tool_choice'] = tool_choice or 'auto'
        key = hashlib.sha256((self.settings.api_url + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        cache = self.cache / (key + '.json')
        if use_cache and cache.exists():
            self.log({'stage': stage, 'status': 'cache_hit', 'request_id': key[:16], 'model': payload['model']})
            return json.loads(cache.read_text(encoding='utf-8'))
        for attempt in range(1, self.settings.attempts + 1):
            start = time.monotonic()
            event = {'stage': stage, 'attempt': attempt, 'request_id': key[:16], 'model': payload['model']}
            retryable = False
            try:
                with httpx.Client(timeout=self.settings.timeout, verify=True) as http:
                    response = http.post(self.settings.api_url,
                        headers={'Authorization': 'Bearer ' + self.settings.api_key}, json=payload)
                event['http_status'] = response.status_code
                if response.status_code >= 400:
                    retryable = response.status_code in (408, 429) or response.status_code >= 500
                    raise APIError('HTTP ' + str(response.status_code) + ': ' + self.safe(response.text))
                raw = response.json()
                if raw.get('status') not in ('completed', None) or raw.get('error'):
                    raise APIError('Response not completed: ' + self.safe(json.dumps({
                        k: raw.get(k) for k in ('status', 'error', 'incomplete_details')})))
                texts, calls = [], []
                for item in raw.get('output', []):
                    if item['type'] == 'message':
                        texts.extend(part['text'] for part in item.get('content', []) if part['type'] == 'output_text')
                    if item['type'] == 'function_call':
                        calls.append({'id': item['call_id'], 'type': 'function', 'function': {
                            'name': item['name'], 'arguments': item['arguments']}})
                if not texts and not calls:
                    raise APIError('No assistant text or function calls')
                usage = raw.get('usage') or {}
                result = {'id': raw.get('id'), 'model': raw.get('model'), 'usage': {
                    'prompt_tokens': usage.get('input_tokens', 0), 'completion_tokens': usage.get('output_tokens', 0),
                    'total_tokens': usage.get('total_tokens', 0), 'details': usage}, 'choices': [{
                    'finish_reason': 'tool_calls' if calls else 'stop', 'message': {
                        'role': 'assistant', 'content': '\n'.join(texts), 'tool_calls': calls}}]}
                event.update(status='success', actual_model=raw.get('model'), usage=result['usage'],
                             response_id=raw.get('id'))
                write_json(cache, result)
                return result
            except (httpx.TransportError, ValueError, APIError) as exc:
                retryable = retryable or isinstance(exc, httpx.TransportError)
                event.update(status='failed', error=self.safe(exc), error_type=type(exc).__name__)
                if not retryable or attempt == self.settings.attempts:
                    raise APIError(self.safe(exc)) from None
            finally:
                event['elapsed_s'] = round(time.monotonic() - start, 3)
                self.log(event)
            self.sleeper(min(10, attempt * 2))
