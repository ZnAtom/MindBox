import json
import os
import re
from pathlib import Path

import httpx

from .config import ROOT


def tokens(text):
    text = re.sub(r'\s+', '', text.lower())
    words = set(re.findall(r'[a-z0-9]+', text))
    return words | {text[i:i + 2] for i in range(len(text) - 1)}


class ToolBox:
    def __init__(self, evidence=None, catalog_path=None):
        self.evidence = evidence or {}
        path = Path(catalog_path or os.getenv('MIND_BOX_CATALOG', ROOT / 'knowledge/catalog.json'))
        if not path.is_absolute():
            path = ROOT / path
        self.catalog = json.loads(path.read_text(encoding='utf-8-sig'))

    def search_catalog(self, query, limit=3):
        query_tokens = tokens(query[:400])
        scored = []
        for row in self.catalog:
            terms = tokens(row['title'] + row['content'])
            overlap = query_tokens & terms
            score = len(overlap) / max(1, len(query_tokens))
            if score:
                scored.append(dict(row, score=round(score, 4)))
        return {'status': 'ok', 'results': sorted(scored, key=lambda r: r['score'], reverse=True)[:max(1, min(5, limit))],
                'score_meaning': 'lexical relevance, not identity probability', 'trust': 'reference_data_only'}

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            return {'status': 'rejected', 'reason': 'arguments must be an object'}
        if name == 'search_catalog':
            if set(arguments) - {'query', 'limit'} or not isinstance(arguments.get('query'), str):
                return {'status': 'rejected', 'reason': 'invalid catalog arguments'}
            limit = arguments.get('limit', 3)
            if type(limit) is not int:
                return {'status': 'rejected', 'reason': 'limit must be an integer'}
            return self.search_catalog(arguments['query'], limit)
        if name == 'get_evidence':
            evidence_id = arguments.get('evidence_id')
            if set(arguments) != {'evidence_id'} or not isinstance(evidence_id, str) or evidence_id not in self.evidence:
                return {'status': 'rejected', 'reason': 'unknown evidence id'}
            return {'status': 'ok', 'evidence': self.evidence[evidence_id]}
        if name == 'search_product_images':
            if set(arguments) != {'query'} or not isinstance(arguments.get('query'), str):
                return {'status': 'rejected', 'reason': 'invalid search arguments'}
            key = os.getenv('SERPAPI_API_KEY')
            if os.getenv('MIND_BOX_WEB_SEARCH', 'false').lower() != 'true' or not key:
                return {'status': 'unavailable', 'reason': 'text image search is not configured; no image uploaded'}
            try:
                r = httpx.get('https://serpapi.com/search.json', params={
                    'engine': 'google_images', 'q': arguments['query'][:200], 'api_key': key}, timeout=30)
                r.raise_for_status()
                return {'status': 'ok', 'kind': 'text_image_search_not_reverse_search',
                        'results': [{k: row.get(k) for k in ('title', 'link', 'thumbnail')}
                                    for row in r.json().get('images_results', [])[:5]],
                        'trust': 'unverified_external_candidates'}
            except (httpx.HTTPError, ValueError) as exc:
                # httpx exception URLs may include the search API key.
                return {'status': 'failed', 'error_type': type(exc).__name__}
        return {'status': 'rejected', 'reason': 'tool not in readonly whitelist'}


TOOL_DEFINITIONS = [
    {'type': 'function', 'function': {'name': 'search_catalog', 'description': 'Read local category/product reference candidates. Relevance is not confidence.',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, 'required': ['query'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {'name': 'get_evidence', 'description': 'Read metadata for an evidence ID supplied in this run; no file access.',
        'parameters': {'type': 'object', 'properties': {'evidence_id': {'type': 'string'}}, 'required': ['evidence_id'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {'name': 'search_product_images', 'description': 'Optional text-based product image search, not reverse image search; may be unavailable.',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}}}
]
