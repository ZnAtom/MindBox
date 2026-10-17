"""Browser voice questions backed by local ASR, SQLite inventory and a remote LLM."""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import wave

from fastapi import FastAPI, HTTPException, Request

from .client import APIError, Client, content
from .config import ROOT, Settings
from .database import Database, DatabaseError, NotFound
from .store import Store


MAX_AUDIO_BYTES = 20 * 1024 * 1024
SAMPLE_RATE = 16_000


class VoiceError(RuntimeError):
    """An expected, user-facing voice pipeline failure."""


SEARCH_INVENTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "search_inventory",
        "description": "查询当前柜体中已确认存在的物品及其抽屉位置。没有结果时不要猜测。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "物品名称、类别或抽屉关键词"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


SYSTEM_PROMPT = """你是 mind box 的语音问答助手。
用户的问题只允许依据工具返回的当前库存回答。涉及物品位置时必须调用 search_inventory，不能凭记忆或猜测。
找不到物品时明确说没有查到，并建议用户检查名称。回答使用简洁中文，不输出 JSON，不声称执行了没有发生的操作。"""


def _decode_audio(payload: bytes) -> bytes:
    """Decode browser WebM/MP4 audio to the WAV format expected by ASR."""
    try:
        import av
        import numpy as np
    except ImportError as error:
        raise VoiceError("后端缺少音频解码依赖，请安装 code/backend/requirements.txt") from error

    try:
        container = av.open(BytesIO(payload))
        stream = next(stream for stream in container.streams if stream.type == "audio")
    except Exception as error:
        raise VoiceError("无法读取浏览器录音格式") from error

    try:
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        pcm_parts = []
        for frame in container.decode(stream):
            converted = resampler.resample(frame)
            if not isinstance(converted, list):
                converted = [converted]
            for output in converted:
                if output is not None:
                    pcm_parts.append(output.to_ndarray().astype(np.int16, copy=False).tobytes())
        flushed = resampler.resample(None)
        if flushed is not None:
            if not isinstance(flushed, list):
                flushed = [flushed]
            for output in flushed:
                if output is not None:
                    pcm_parts.append(output.to_ndarray().astype(np.int16, copy=False).tobytes())
    finally:
        container.close()

    pcm = b"".join(pcm_parts)
    if not pcm:
        raise VoiceError("录音中没有可识别的音频")
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    return output.getvalue()


class LocalTranscriber:
    """Lazy local faster-whisper worker; the audio never leaves this process."""

    def __init__(self, model=None, models=None):
        self.model_name = model or os.getenv("MIND_BOX_ASR_MODEL", "small")
        self.models = Path(models or os.getenv("MIND_BOX_ASR_MODELS", ROOT / "var/models"))
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except ImportError as error:
            raise VoiceError("后端缺少 faster-whisper，请安装 code/backend/requirements-asr.txt") from error
        model_path = Path(self.model_name)
        if not model_path.is_dir():
            try:
                model_path = Path(download_model(self.model_name, cache_dir=str(self.models), local_files_only=True))
            except Exception as error:
                raise VoiceError("本地 ASR 模型不存在，请先下载到 code/backend/var/models") from error
        self._model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=4)
        return self._model

    def transcribe(self, wav_bytes: bytes) -> dict:
        with self._lock:
            model = self._load()
            with tempfile.NamedTemporaryFile(suffix=".wav") as source:
                source.write(wav_bytes)
                source.flush()
                segments, info = model.transcribe(str(source.name), language="zh", beam_size=5,
                                                  vad_filter=True, condition_on_previous_text=False)
                values = [{"id": item.id, "start_ms": round(item.start * 1000),
                           "end_ms": round(item.end * 1000), "text": item.text.strip()}
                          for item in segments]
        return {"text": "".join(item["text"] for item in values),
                "segments": values, "language": info.language}


class VoiceAssistant:
    def __init__(self, database=None, cabinet_id="cabinet-01", settings=None,
                 transcriber=None, client=None, output=None):
        self.database = Path(database or os.getenv("MIND_BOX_DATABASE", ROOT / "var/mind-box.sqlite3"))
        self.cabinet_id = cabinet_id
        self.settings = settings or Settings.load()
        self.store = Store(Database(self.database))
        self.transcriber = transcriber or LocalTranscriber()
        self.client = client or Client(self.settings, output or ROOT / "var/voice-api")

    def _search_inventory(self, query, cabinet_id=None):
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 120:
            raise VoiceError("库存查询关键词无效")
        query = query.strip()
        cabinet_id = cabinet_id or self.cabinet_id
        layer = next((index for index, label in enumerate(("第一层", "第二层", "第三层"), 1)
                      if label in query), None)
        try:
            snapshot = self.store.get_inventory_snapshot(cabinet_id, query=query)
            if not snapshot["items"] and layer:
                snapshot = self.store.get_inventory_snapshot(
                    cabinet_id, location_id=f"{cabinet_id}:drawer-{layer}")
        except NotFound as error:
            raise VoiceError("库存数据库尚未初始化，请先运行 db init") from error
        results = []
        for item in snapshot["items"]:
            location = item.get("location_id") or ""
            drawer = location.rsplit(":drawer-", 1)[-1] if ":drawer-" in location else None
            drawer_names = {"1": "第一层", "2": "第二层", "3": "第三层"}
            results.append({"id": item["id"], "name": item["name"], "category": item["category"],
                            "drawer": drawer_names.get(drawer, "位置未知"),
                            "presence_status": item["presence_status"],
                            "expires_on": item.get("expires_on")})
        return {"status": "ok", "query": query, "inventory_known": snapshot["inventory_known"],
                "items": results}

    def query(self, audio_bytes: bytes, cabinet_id=None) -> dict:
        if not audio_bytes or len(audio_bytes) > MAX_AUDIO_BYTES:
            raise VoiceError("录音为空或超过 20 MB 限制")
        if not self.settings.api_key:
            raise VoiceError("尚未配置远程 AI 密钥，请设置 MIND_BOX_API_KEY")
        if not self.settings.api_url:
            raise VoiceError("尚未配置远程 AI 地址，请设置 MIND_BOX_API_URL")
        wav_bytes = _decode_audio(audio_bytes)
        transcript = self.transcriber.transcribe(wav_bytes)
        text = transcript.get("text", "").strip()
        if not text:
            raise VoiceError("没有识别到清晰的人声")

        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text}]
        try:
            response = self.client.chat(
                messages, "voice_inventory_tool", max_tokens=600,
                tools=[SEARCH_INVENTORY_TOOL],
                tool_choice={"type": "function", "function": {"name": "search_inventory"}},
                use_cache=False,
            )
        except APIError as error:
            raise VoiceError("远程 AI 请求失败：" + self.client.safe(error)) from error
        message = response["choices"][0].get("message", {})
        calls = message.get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "search_inventory":
            raise VoiceError("远程 AI 没有按约定查询库存")
        try:
            arguments = json.loads(calls[0]["function"]["arguments"])
            inventory = self._search_inventory(arguments.get("query"), cabinet_id)
        except (KeyError, TypeError, ValueError) as error:
            raise VoiceError("远程 AI 返回了无效的库存查询") from error
        messages += [
            {"role": "assistant", "content": message.get("content"), "tool_calls": calls},
            {"role": "tool", "tool_call_id": calls[0]["id"],
             "content": json.dumps(inventory, ensure_ascii=False)},
        ]
        try:
            answer = self.client.chat(messages, "voice_inventory_answer", max_tokens=400, use_cache=False)
        except APIError as error:
            raise VoiceError("远程 AI 回答失败：" + self.client.safe(error)) from error
        text_answer = content(answer).strip()
        if not text_answer:
            raise VoiceError("远程 AI 返回了空回答")
        return {"transcript": text, "answer": text_answer, "inventory": inventory}


def create_app(assistant=None):
    app = FastAPI(title="Mind Box voice service")
    service = assistant or VoiceAssistant()

    @app.get("/api/voice/health")
    def health():
        return {"ok": True, "remote_ai_configured": bool(service.settings.api_key and service.settings.api_url),
                "database": str(service.database)}

    @app.post("/api/voice/query")
    async def query(request: Request):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "录音超过 20 MB 限制")
        payload = await request.body()
        try:
            return service.query(payload, request.headers.get("x-mind-box-cabinet"))
        except VoiceError as error:
            raise HTTPException(503, str(error)) from error
        except (DatabaseError, OSError) as error:
            raise HTTPException(500, "语音服务内部错误：" + str(error)) from error

    return app


app = create_app()
