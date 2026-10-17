"""Frozen structured-output loop used by the original experiment's pipeline.

Kept local so frozen-image inference does not import changing production media code.
"""
from pydantic import ValidationError

from app.client import content, parse_json


def structured(client, messages, stage, schema, validate=None, max_tokens=5000):
    error = None
    for repair in range(2):
        response = client.chat(messages, stage + (f'_repair{repair}' if repair else ''),
                               json_mode=True, max_tokens=max_tokens)
        try:
            value = schema.model_validate(parse_json(content(response)))
            if validate:
                validate(value)
            return value
        except (ValueError, ValidationError) as exc:
            error = client.safe(exc)
            client.log({'stage': stage, 'status': 'invalid_output', 'repair': repair, 'error': error})
            messages = messages + [{'role': 'assistant', 'content': content(response)},
                {'role': 'user', 'content': 'JSON格式或引用不符合约定，请修正并完整重发，不要添加新物品。错误：' + error}]
    raise ValueError('Structured output invalid: ' + str(error))
