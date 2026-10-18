import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.store import Store
from app import voice_api


def test_local_transcriber_passes_normalized_samples_without_reopening_a_file(monkeypatch):
    buffer = BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        output.writeframes(np.array([-32768, 0, 32767], dtype='<i2').tobytes())

    class Model:
        def transcribe(self, audio, **options):
            np.testing.assert_array_equal(audio, np.array([-1, 0, 32767 / 32768], dtype=np.float32))
            assert options == {'language': 'zh', 'beam_size': 5, 'vad_filter': True,
                               'condition_on_previous_text': False}
            return iter([SimpleNamespace(id=0, start=0, end=.5, text=' test ')]), SimpleNamespace(language='zh')

    transcriber = voice_api.LocalTranscriber()
    monkeypatch.setattr(transcriber, '_load', lambda: Model())
    result = transcriber.transcribe(buffer.getvalue())
    assert result == {'text': 'test', 'language': 'zh',
                      'segments': [{'id': 0, 'start_ms': 0, 'end_ms': 500, 'text': 'test'}]}


@pytest.mark.parametrize('channels,width,rate', [(2, 2, 16000), (1, 1, 16000), (1, 2, 8000)])
def test_local_transcriber_rejects_unnormalized_audio_before_loading(monkeypatch, channels, width, rate):
    buffer = BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(bytes(channels * width * 10))
    transcriber = voice_api.LocalTranscriber()
    monkeypatch.setattr(transcriber, '_load', lambda: pytest.fail('invalid audio must not load the model'))
    with pytest.raises(voice_api.VoiceError, match='16 kHz'):
        transcriber.transcribe(buffer.getvalue())


class FakeTranscriber:
    def transcribe(self, wav_bytes):
        assert wav_bytes == b"wav"
        return {"text": "鱼油放在哪里", "segments": []}


class FakeClient:
    def __init__(self):
        self.calls = []

    def safe(self, value):
        return str(value)

    def chat(self, messages, stage, **kwargs):
        self.calls.append((stage, messages, kwargs))
        if stage == "voice_inventory_tool":
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "search_inventory", "arguments": json.dumps({"query": "鱼油"}, ensure_ascii=False)
                }
            }]}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "鱼油在第二层。"}}]}


@pytest.fixture
def inventory(tmp_path):
    store = Store(tmp_path / "var/mind-box.sqlite3")
    store.initialize(known_empty=True)
    store.create_item(
        "cabinet-01",
        {"name": "鱼油", "category": "日用品", "location_id": "cabinet-01:drawer-2"},
        0, "add-fish-oil", "test",
    )
    return store


def test_search_inventory_returns_real_drawer(inventory):
    assistant = voice_api.VoiceAssistant(
        database=inventory.db.path,
        settings=Settings("https://example.invalid", "secret", "test"),
        transcriber=FakeTranscriber(),
        client=FakeClient(),
    )

    result = assistant._search_inventory("鱼油")

    assert result["items"] == [{
        "id": result["items"][0]["id"],
        "name": "鱼油",
        "category": "日用品",
        "drawer": "第二层",
        "presence_status": "present",
        "expires_on": None,
    }]


def test_query_uses_remote_tool_and_returns_answer(inventory, monkeypatch):
    client = FakeClient()
    assistant = voice_api.VoiceAssistant(
        database=inventory.db.path,
        settings=Settings("https://example.invalid", "secret", "test"),
        transcriber=FakeTranscriber(),
        client=client,
    )
    monkeypatch.setattr(voice_api, "_decode_audio", lambda payload: b"wav")

    result = assistant.query(b"browser-audio")

    assert result["transcript"] == "鱼油放在哪里"
    assert result["answer"] == "鱼油在第二层。"
    assert result["inventory"]["items"][0]["drawer"] == "第二层"
    assert [call[0] for call in client.calls] == ["voice_inventory_tool", "voice_inventory_answer"]


def test_local_asr_reads_current_inventory_vocabulary_for_each_recording(inventory, monkeypatch):
    assistant = voice_api.VoiceAssistant(database=inventory.db.path,
        settings=Settings('https://example.invalid', 'secret', 'test'), client=FakeClient())
    calls = []
    def wav_payload():
        buffer = BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0\0' * 160)
        return buffer.getvalue()

    class Model:
        def transcribe(self, audio, **options):
            assert audio.shape == (160,)
            calls.append(options)
            return [], SimpleNamespace(language='zh')
    monkeypatch.setattr(assistant.transcriber, '_load', lambda: Model())
    assistant.transcriber.transcribe(wav_payload())
    inventory.create_item('cabinet-01', {'name': '维生素'}, 1, 'vitamins', 'test')
    assistant.transcriber.transcribe(wav_payload())
    assert calls[0]['hotwords'] == '鱼油'
    assert '维生素' not in calls[0]['initial_prompt']
    assert '鱼油' in calls[1]['hotwords'] and '维生素' in calls[1]['hotwords']
    assert '简体中文' in calls[1]['initial_prompt']


def test_missing_remote_key_fails_before_audio_processing(tmp_path, monkeypatch):
    assistant = voice_api.VoiceAssistant(
        database=tmp_path / "missing.sqlite3",
        settings=Settings("https://example.invalid", "", "test"),
        transcriber=FakeTranscriber(),
        client=FakeClient(),
    )
    monkeypatch.setattr(voice_api, "_decode_audio", lambda payload: pytest.fail("audio should not be decoded"))

    with pytest.raises(voice_api.VoiceError, match="MIND_BOX_API_KEY"):
        assistant.query(b"browser-audio")


def test_http_endpoint_accepts_raw_browser_audio():
    class StubAssistant:
        settings = SimpleNamespace(api_key="key", api_url="https://example.invalid")
        database = Path("/tmp/mind-box-test.sqlite3")

        def query(self, payload, cabinet_id=None):
            assert payload == b"browser-audio"
            assert cabinet_id == "cabinet-01"
            return {"transcript": "鱼油放在哪里", "answer": "鱼油在第二层。", "inventory": {}}

    response = TestClient(voice_api.create_app(StubAssistant())).post(
        "/api/voice/query", content=b"browser-audio",
        headers={"content-type": "audio/webm", "x-mind-box-cabinet": "cabinet-01"},
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "鱼油在第二层。"
