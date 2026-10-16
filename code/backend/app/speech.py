"""Final-transcript evidence, conservative item linking and explicit date slots."""
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Literal

import dateparser
from dateutil.relativedelta import relativedelta
from pydantic import Field

from .client import write_json
from .database import Conflict, DatabaseError, encode, fingerprint, timestamp, utcnow
from .enrichment import Enrichment
from .experiments import file_hash
from .inventory_schemas import Strict
from .store import require


class CommandSlot(Strict):
    kind: Literal['before_expiry', 'after_expiry_remove']
    raw: str = Field(min_length=1, max_length=300)


class SpeechSlots(Strict):
    subject_raw: str | None
    production_raw: str | None
    expiry_raw: str | None
    shelf_life_raw: str | None
    commands: list[CommandSlot] = Field(max_length=2)
    uncertainties: list[str] = Field(max_length=20)


def extract_slots(text, client, output):
    from .pipeline import structured
    prompt = (Path(__file__).parent / 'prompts/speech.txt').read_text(encoding='utf-8')

    def grounded(value):
        validate_slots(text, value.model_dump())

    slots = structured(client, [{'role': 'system', 'content': prompt},
        {'role': 'user', 'content': encode({'asr_final_text': text})}], 'speech_slots', SpeechSlots,
        grounded, max_tokens=1500).model_dump()
    write_json(output, {'slots': slots, 'prompt_sha256': fingerprint(prompt), 'source_text': text})
    return slots


def validate_slots(text, slots):
    slots = SpeechSlots.model_validate(slots).model_dump()
    raw = [slots[f] for f in ('subject_raw', 'production_raw', 'expiry_raw', 'shelf_life_raw') if slots[f]]
    raw += [c['raw'] for c in slots['commands']]
    if any(value not in text for value in raw) or len({c['kind'] for c in slots['commands']}) != len(slots['commands']):
        raise DatabaseError('Every speech slot must quote final transcript evidence exactly once')
    return slots


def normalize_date(raw, clock=None):
    if not raw:
        return {'raw': None, 'value': None, 'missing': []}
    match = re.fullmatch(r'(?:(\d{4})年|(今年|去年|明年))?(\d{1,2})月(\d{1,2})[日号]?', raw.strip())
    iso = re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw.strip())
    if not match and not iso:
        return {'raw': raw, 'value': None, 'missing': ['unparsed_date']}
    normalized = raw
    if match:
        year, relative, month, day = match.groups()
        if relative:
            if not clock or clock.get('quality') not in ('trusted', 'synthetic_test') or not clock.get('capture_start'):
                return {'raw': raw, 'value': None, 'month': int(month), 'day': int(day), 'missing': ['capture_year']}
            capture = datetime.fromisoformat(timestamp(clock['capture_start']))
            # Preserve the capture's declared local year rather than UTC's boundary year.
            capture = datetime.fromisoformat(clock['capture_start'].replace('Z', '+00:00'))
            year = str(capture.year + {'今年': 0, '去年': -1, '明年': 1}[relative])
        if not year:
            return {'raw': raw, 'value': None, 'month': int(month), 'day': int(day), 'missing': ['explicit_year']}
        normalized = f'{year}-{int(month):02d}-{int(day):02d}'
    value = dateparser.parse(normalized, languages=['zh'], settings={'STRICT_PARSING': True, 'DATE_ORDER': 'YMD'})
    return {'raw': raw, 'value': value.date().isoformat() if value else None,
            'missing': [] if value else ['invalid_date'], 'clock_quality': (clock or {}).get('quality', 'unknown')}


def normalized_facts(slots, clock=None):
    production, expiry = normalize_date(slots['production_raw'], clock), normalize_date(slots['expiry_raw'], clock)
    missing = production['missing'] + expiry['missing']
    raw = slots['shelf_life_raw']
    duration = None
    if raw:
        match = re.fullmatch(r'(?:保质期)?\s*(\d+|一|二|两|三|六|十|十二)\s*(年|个月|月|天|日)', raw)
        if match:
            number, unit = match.groups()
            number = int(number) if number.isdecimal() else {'一': 1, '二': 2, '两': 2, '三': 3, '六': 6, '十': 10, '十二': 12}[number]
            if 0 < number <= 3650:
                duration = {'amount': number, 'unit': unit}
        if not duration:
            missing.append('shelf_life_needs_review')
    if slots.get('uncertainties'):
        production['value'] = expiry['value'] = None
        duration = None
        missing.append('semantic_needs_review')
    derived_expiry = None
    if production['value'] and duration and not expiry['value']:
        start = datetime.fromisoformat(production['value'])
        units = {'年': 'years', '个月': 'months', '月': 'months', '天': 'days', '日': 'days'}
        derived_expiry = (start + relativedelta(**{units[duration['unit']]: duration['amount']})).date().isoformat()
    return {'production': production, 'expiry': expiry, 'duration': duration,
            'derived_expiry': derived_expiry, 'missing': sorted(set(missing)),
            'note': 'Calendar anniversary is a date candidate; expiry cutoff needs an explicit boundary policy.'}


class SpeechService:
    def __init__(self, context, cabinet_id='cabinet-01'):
        self.context, self.store, self.cabinet_id = context, context.store, cabinet_id

    def ingest(self, source_video_hash, audio, asr, slots, audio_offset_ms=0):
        if source_video_hash not in {v['sha256'] for v in self.context.manifest['videos']}:
            raise DatabaseError('Speech source is outside this experiment')
        if asr.get('status') != 'completed' or asr.get('audio_sha256') != file_hash(audio):
            raise DatabaseError('A completed ASR result for the exact archived audio is required')
        slots = validate_slots(asr['text'], slots)
        segments = asr.get('segments', [])
        if not segments or type(audio_offset_ms) is not int:
            raise DatabaseError('Final timed ASR segments and an integer PTS offset are required')
        start, end = min(s['start_ms'] for s in segments) + audio_offset_ms, max(s['end_ms'] for s in segments) + audio_offset_ms
        if start < 0 or end <= start:
            raise DatabaseError('Invalid speech time range')
        identity = 'speech-' + fingerprint([self.cabinet_id, source_video_hash, asr['audio_sha256']])[:32]
        clock = self.context.manifest['capture_clock'] or {}
        parsed = {'slots': slots, 'facts': normalized_facts(slots, clock), 'asr': asr, 'link': None}
        body_hash = fingerprint([asr['text'], slots, start, end, clock])
        asset, digest = self.store._archive(Path(audio).resolve().parent, Path(audio).name)
        with self.store.db.transaction() as connection:
            existing = connection.execute('SELECT * FROM speech_utterances WHERE id=?', (identity,)).fetchone()
            if existing:
                if existing['body_hash'] != body_hash:
                    raise Conflict('Final ASR revision needs explicit revalidation; existing facts/tasks were preserved')
                return self._value(dict(existing))
            connection.execute('INSERT INTO speech_utterances VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, ?, ?)',
                (identity, self.cabinet_id, source_video_hash, asset, digest, start, end, asr['text'],
                 'faster-whisper:' + asr['model'], encode(clock), encode(parsed), 'pending', body_hash, utcnow()))
        return self.get(identity)

    @staticmethod
    def _value(row):
        return {**row, 'parsed': json.loads(row['parsed_json']), 'clock': json.loads(row['clock_json'])}

    def get(self, identity):
        with self.store.db.connect() as connection:
            row = require(connection, 'speech_utterances', identity)
            if row['cabinet_id'] != self.cabinet_id:
                raise DatabaseError('Wrong speech cabinet')
            return self._value(row)

    def link(self, identity):
        utterance = self.get(identity)
        subject = utterance['parsed']['slots']['subject_raw']
        # Batch A supports one newly inserted matching item in the same video only.
        candidates = []
        with self.store.db.connect() as connection:
            rows = connection.execute('SELECT DISTINCT i.* FROM items i JOIN item_observations io ON io.item_id=i.id '
                'JOIN observations o ON o.id=io.observation_id JOIN processing_runs r ON r.id=o.run_id '
                'JOIN operations p ON p.id=r.operation_id JOIN media_captures m ON m.id=p.capture_id '
                'WHERE i.cabinet_id=? AND m.input_sha256=?', (self.cabinet_id, utterance['source_video_hash']))
            for row in rows:
                item = self.store.get_item(row['id'])
                events = connection.execute('SELECT e.payload_json FROM verified_events e JOIN inventory_commits c ON c.id=e.commit_id '
                    'JOIN processing_runs r ON r.id=c.run_id JOIN operations p ON p.id=r.operation_id '
                    'JOIN media_captures m ON m.id=p.capture_id WHERE e.item_id=? AND m.input_sha256=?',
                    (item['id'], utterance['source_video_hash']))
                intervals = [e['event_window_ms'] for e in (json.loads(r[0]) for r in events) if e['kind'] == 'add']
                close = any(end >= utterance['start_ms'] - 5000 and start <= utterance['end_ms'] for start, end in intervals)
                text = ' '.join(str(item.get(f) or '') for f in ('name', 'description', 'brand', 'variant'))
                if subject and subject in text and close and item['presence_status'] == 'present':
                    candidates.append(item['id'])
        linked = candidates[0] if len(candidates) == 1 else None
        if utterance['item_id'] and utterance['item_id'] != linked:
            raise Conflict('Speech already linked; changed association needs review')
        parsed = {**utterance['parsed'], 'link': {'candidates': candidates, 'subject': subject,
                  'policy': 'same-video-single-new-item-with-type-and-time', 'reason': None if linked else 'ambiguous_or_no_confirmed_item'}}
        with self.store.db.transaction() as connection:
            connection.execute('UPDATE speech_utterances SET item_id=?, link_status=?, parsed_json=? WHERE id=?',
                               (linked, 'linked' if linked else 'pending', encode(parsed), identity))
        return self.get(identity)

    def apply_facts(self, identity):
        utterance = self.get(identity)
        if utterance['link_status'] != 'linked':
            return {'status': 'pending', 'reason': 'speech_item_link_unresolved'}
        facts, patch, provenance = utterance['parsed']['facts'], {}, {}
        reference = 'speech:' + identity
        for field, value, source in (
            ('production_on', facts['production']['value'], 'speech'),
            ('expires_on', facts['expiry']['value'] or facts['derived_expiry'], 'speech' if facts['expiry']['value'] else 'derived'),
            ('shelf_life_raw', utterance['parsed']['slots']['shelf_life_raw'], 'speech')):
            if value:
                patch[field] = value
                provenance[field] = {'source': source, 'certainty': 'recorded', 'evidence_refs': [reference],
                                     'extractor': 'speech-slots-v1'}
        if not patch:
            return {'status': 'needs_input', 'missing': facts['missing']}
        with self.store.db.connect() as connection:
            existing = connection.execute('SELECT receipt_json FROM inventory_commits WHERE cabinet_id=? AND idempotency_key=?',
                (self.cabinet_id, 'speech-facts:' + identity + ':' + str(utterance['version']))).fetchone()
        if existing:
            return json.loads(existing[0])
        item = self.store.get_item(utterance['item_id'])
        # Conflicting existing facts need review; neither source wins automatically.
        if any(item.get(k) is not None and item[k] != v for k, v in patch.items()):
            return {'status': 'pending', 'reason': 'speech_conflicts_with_existing_fact', 'suggestions': patch}
        return Enrichment(self.store).apply(item['id'], patch, provenance, item['version'],
            self.store.get_inventory_snapshot()['inventory_revision'], 'speech-facts:' + identity + ':' + str(utterance['version']),
            facts=True, cabinet_id=self.cabinet_id)
