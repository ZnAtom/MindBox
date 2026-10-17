"""Protocol and isolation checks; no live API requests."""
import json

import httpx

from app.config import Settings
from model_comparison.responses_client import ResponsesClient, response_input


def test_function_result_and_images_keep_protocol_identity():
    messages = [
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AA=='}}]},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-7', 'function': {
            'name': 'get_frames', 'arguments': '{"fps":8}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-7', 'content': '{"frames":[]}'}]
    items = response_input(messages)
    assert items[0]['content'][0] == {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,AA==', 'detail': 'high'}
    assert items[1]['call_id'] == items[2]['call_id'] == 'call-7'
    assert items[1]['name'] == 'get_frames'
    assert items[2]['type'] == 'function_call_output'


def test_store_disabled_and_function_call_roundtrip(monkeypatch, tmp_path):
    observed = []
    transport = httpx.MockTransport(lambda request: (
        observed.append(json.loads(request.content)), httpx.Response(200, json={
            'id': 'response-test', 'model': 'gateway-reported-model', 'status': 'completed',
            'output': [{'type': 'function_call', 'call_id': 'call-7', 'name': 'get_frames', 'arguments': '{}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}))[1])
    original = httpx.Client
    monkeypatch.setattr('model_comparison.responses_client.httpx.Client',
                        lambda **kwargs: original(transport=transport, **kwargs))
    client = ResponsesClient(Settings('https://example.test/responses', 'secret-placeholder', 'requested-model'), tmp_path)
    result = client.chat([{'role': 'user', 'content': 'read frames'}], 'tool_test', tools=[{
        'type': 'function', 'function': {'name': 'get_frames', 'parameters': {'type': 'object'}}}],
        tool_choice={'type': 'function', 'function': {'name': 'get_frames'}})
    assert observed[0]['store'] is False
    assert observed[0]['tool_choice'] == {'type': 'function', 'name': 'get_frames'}
    assert result['choices'][0]['message']['tool_calls'][0]['id'] == 'call-7'
    assert result['model'] == 'gateway-reported-model'
    assert 'secret-placeholder' not in (tmp_path / 'api-attempts.jsonl').read_text()
