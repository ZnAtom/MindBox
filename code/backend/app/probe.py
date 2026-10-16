from pathlib import Path
from PIL import Image, ImageDraw

from .client import APIError, Client, content, image_part, parse_json, write_json
from .config import Settings


def probe(output, model=None):
    output = Path(output)
    settings = Settings.load()
    if model:
        settings.model = model
    client = Client(settings, output)
    result = {'requested_model': client.settings.model, 'endpoint': client.settings.api_url,
              'tls_verified': client.settings.verify_tls, 'capabilities': {}}
    im = Image.new('RGB', (480, 360), 'white')
    ImageDraw.Draw(im).rectangle((90, 70, 340, 280), fill='red')
    path = output / 'vision-probe.jpg'
    im.save(path)
    tests = [
        ('text', [{'role': 'user', 'content': '只回复 OK'}], {}),
        ('vision_json', [{'role': 'user', 'content': [
            {'type': 'text', 'text': '观察图片，只输出JSON，格式 {"shape":"看到的形状","color":"形状颜色"}。'},
            image_part(path)]}], {'json_mode': True}),
        ('native_tools', [{'role': 'user', 'content': '必须调用 search_catalog 查询饮品，然后等待工具结果。'}],
         {'tools': [{'type': 'function', 'function': {'name': 'search_catalog',
             'description': '检索本地类别说明', 'parameters': {'type': 'object',
             'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}}}],
          'tool_choice': {'type': 'function', 'function': {'name': 'search_catalog'}}})]
    for name, messages, kwargs in tests:
        try:
            response = client.chat(messages, 'probe_' + name, max_tokens=600, **kwargs)
            msg = response['choices'][0]['message']
            entry = {'status': 'responded', 'actual_model': response.get('model'),
                     'content': msg.get('content'), 'tool_calls': msg.get('tool_calls')}
            if name == 'vision_json':
                entry['parsed'] = parse_json(content(response))
                entry['semantic_checks'] = {
                    'color_red': 'red' in str(entry['parsed']).lower() or '红' in str(entry['parsed']),
                    'shape_rectangle': any(word in str(entry['parsed']).lower() for word in ('长方形', '矩形', 'rectangle'))}
            if name == 'native_tools':
                entry['status'] = 'supported' if msg.get('tool_calls') else 'no_tool_call'
            result['capabilities'][name] = entry
        except (APIError, ValueError) as exc:
            result['capabilities'][name] = {'status': 'failed', 'error': client.safe(exc)}
        write_json(output / 'capabilities.json', result)
        print(name, result['capabilities'][name]['status'], flush=True)
    return result
