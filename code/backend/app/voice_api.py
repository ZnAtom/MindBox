"""Browser voice questions backed by local ASR, SQLite inventory and a remote LLM."""

from __future__ import annotations

from io import BytesIO
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import wave
import re
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .client import APIError, Client, content
from .config import ROOT, Settings
from .database import Database, DatabaseError, NotFound, encode, fingerprint, new_id, utcnow
from .inventory_schemas import Strict
from .live_tasks import LiveTasks, VOICE_TOOLS
from .store import Store
from pydantic import Field


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


SYSTEM_PROMPT = """你是 mind box 的统一语音与文字助手。输入可能是按钮提问、开门期间讲话或文字。
只依据真实 SQLite 库存及工具执行回执回答。查位置必须 search_inventory，不能猜测。
search_inventory.query 只填物品名或抽屉关键词，不要把“在哪里”等问句一起传入。
明确陈述生产日期、到期日期、保质期时用 record_item_facts；raw 字段必须逐字引用原文。
subject_raw 保留原话中区分抽屉的限定语，例如“第一层的坚果”。
提醒需调用 schedule_reminder；先查关联物品的真实日期，再计算到期前提醒的时间。
“明天”“十分钟后”等相对时间以 speech_clock.capture_start 为基准；没有讲话时钟时才用 now。
只有保质期没有生产日期时，保存保质期，不要把放入时间当生产日期。缺少年份请追问。
用户同时交代信息与提醒时，依次完成两项。只有在工具实际成功后才说已保存、已设置。
“这个”只允许关联本次收纳唯一的物品。多件同名、信息不足、冲突或条件式任务无法执行时明确说明并追问。
定时任务支持一次和每天；条件式任务（例如取出后再提醒）暂不支持，不声称已设置。
只回应用户实际的问题或交代；背景电视、闲聊不创建任务。对开门录音可返回空回答。
简洁中文回答，不输出 JSON。"""


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
        self.settings = settings or Settings.load()
        self.database = Path(database or os.getenv("MIND_BOX_DATABASE", ROOT / "var/mind-box.sqlite3")).resolve()
        self.cabinet_id = cabinet_id
        self.store = Store(Database(self.database))
        self.tasks = LiveTasks(self.store, os.getenv('MIND_BOX_TIMEZONE', 'Asia/Shanghai'))
        self.transcriber = transcriber or LocalTranscriber()
        self.output = Path(output or self.database.parent / 'voice-api')
        self.client = client or Client(self.settings, self.output)

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

    def configured(self):
        if not self.settings.api_key:
            raise VoiceError("尚未配置远程 AI 密钥，请设置 MIND_BOX_API_KEY")
        if not self.settings.api_url:
            raise VoiceError("尚未配置远程 AI 地址，请设置 MIND_BOX_API_URL")

    def begin(self, cabinet, origin, *, session=None, audio_session=None, audio_path=None, state='processing'):
        identity = new_id()
        with self.store.db.transaction() as db:
            db.execute('INSERT INTO voice_interactions '
                '(id,cabinet_id,origin,session_id,audio_session_id,audio_path,state,created_at) VALUES (?,?,?,?,?,?,?,?)',
                (identity, cabinet, origin, session, audio_session, str(audio_path) if audio_path else None, state, utcnow()))
        return identity

    def save_result(self, identity, result, state='completed'):
        with self.store.db.transaction() as db:
            db.execute('UPDATE voice_interactions SET state=?,transcript=?,result_json=? WHERE id=?',
                       (state, result.get('transcript', ''), encode(result), identity))

    def query(self, audio_bytes: bytes, cabinet_id=None) -> dict:
        if not audio_bytes or len(audio_bytes) > MAX_AUDIO_BYTES:
            raise VoiceError("录音为空或超过 20 MB 限制")
        self.configured()
        cabinet = cabinet_id or self.cabinet_id
        wav_bytes = _decode_audio(audio_bytes)
        transcript = self.transcriber.transcribe(wav_bytes)
        identity = self.begin(cabinet, 'button')
        path = self.output / identity / 'audio.wav'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav_bytes)
        return self.understand(transcript.get('text', ''), cabinet, identity, audio=path)

    def text_query(self, text, cabinet_id=None):
        cabinet = cabinet_id or self.cabinet_id
        identity = self.begin(cabinet, 'text')
        raw = text.strip()
        if re.search(r'(在哪里|在哪儿|在哪|放哪|位置)', raw) and not re.search(r'(提醒|保质期|生产日期|到期|取出|放入|记录)', raw):
            term = re.sub(r'[？?。！!，,]', '', raw)
            term = re.sub(r'请问|帮我找一下|帮我找找|帮我找|我想找|我的|在哪里|在哪儿|在哪|放哪了|放在哪里了|放在哪里|找一下|找找', '', term).strip()
            inventory = self._search_inventory(term, cabinet)
            if inventory['items']:
                answer = '\n'.join(f"{item['name']}在{item['drawer']}。" for item in inventory['items'])
            else:
                answer = f'没有查到“{term}”，请检查物品名称。'
            result = {'id': identity, 'transcript': raw, 'answer': answer, 'inventory': inventory, 'operations': []}
            self.save_result(identity, result)
            return result
        self.configured()
        return self.understand(raw, cabinet, identity)

    def dispatch(self, name, arguments, text, cabinet, identity, scope, clock, audio):
        if name == 'search_inventory':
            return self._search_inventory(arguments.get('query'), cabinet)
        if name == 'record_item_facts':
            return self.tasks.record_facts(arguments, text, cabinet, identity, scope=scope, clock=clock, audio=audio)
        if name == 'schedule_reminder':
            evidence = arguments.get('evidence')
            if not evidence or evidence not in text:
                raise DatabaseError('提醒必须引用用户原话')
            item = None
            if arguments.get('item_query'):
                item, problem = self.tasks.resolve_item(arguments['item_query'], cabinet, scope)
                if problem:
                    return problem
            return self.tasks.schedule(cabinet, arguments['message'], arguments['due_at'],
                'voice:' + fingerprint([identity, arguments]), arguments['repeat'], item['id'] if item else None)
        if name == 'list_reminders':
            return {'reminders': self.tasks.reminders(cabinet)}
        if name == 'cancel_reminder':
            if not arguments.get('evidence') or arguments['evidence'] not in text:
                raise DatabaseError('取消提醒必须引用用户原话')
            return self.tasks.cancel(arguments['reminder_id'], cabinet)
        raise DatabaseError('不支持的语音操作')

    def understand(self, text, cabinet, identity, *, scope=None, clock=None, audio=None, allow_silence=False):
        operations, inventory = [], {}
        text = text.strip()
        result = {'id': identity, 'transcript': text, 'answer': '', 'inventory': inventory, 'operations': operations}
        try:
            if not text:
                if allow_silence:
                    self.save_result(identity, {**result, 'status': 'no_speech'})
                    return result
                raise VoiceError('没有识别到清晰的人声')
            self.configured()
            snapshot = self.store.get_inventory_snapshot(cabinet)
            context = {'now': datetime.now(ZoneInfo(self.tasks.timezone)).isoformat(),
                'speech_clock': clock, 'session_item_ids': scope or [],
                'inventory': [{k: item[k] for k in ('id', 'name', 'description', 'category', 'location_id',
                    'production_on', 'expires_on', 'shelf_life_raw')} for item in snapshot['items']]}
            messages = [{'role': 'system', 'content': SYSTEM_PROMPT + '\n当前上下文：' + encode(context)},
                        {'role': 'user', 'content': text}]
            for turn in range(7):
                response = self.client.chat(messages, 'voice_inventory_tool' if turn == 0 else 'voice_inventory_answer',
                    max_tokens=1200, tools=[SEARCH_INVENTORY_TOOL] + VOICE_TOOLS,
                    tool_choice='auto', use_cache=False)
                message = response['choices'][0].get('message', {})
                calls = message.get('tool_calls') or []
                if not calls:
                    result.update(answer=content(response).strip(), inventory=inventory)
                    self.save_result(identity, result)
                    return result
                messages.append({'role': 'assistant', 'content': message.get('content'), 'tool_calls': calls})
                for call in calls:
                    name = call.get('function', {}).get('name')
                    try:
                        arguments = json.loads(call['function']['arguments'])
                        if not isinstance(arguments, dict):
                            raise ValueError('工具参数必须是对象')
                        value = self.dispatch(name, arguments, text, cabinet, identity, scope, clock, audio)
                    except (DatabaseError, KeyError, TypeError, ValueError) as error:
                        value = {'status': 'needs_input', 'reason': str(error)}
                    operations.append({'tool': name, 'result': value})
                    if name == 'search_inventory':
                        inventory = value
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': encode(value)})
            raise VoiceError('一次交代包含过多操作，请拆分后重试')
        except Exception as error:
            message = self.client.safe(error) if isinstance(error, APIError) else str(error)
            result.update(error=message, inventory=inventory)
            self.save_result(identity, result, 'failed')
            if isinstance(error, APIError):
                raise VoiceError('远程 AI 请求失败：' + message) from error
            raise


class TextCommand(Strict):
    text: str = Field(min_length=1, max_length=4000)
    cabinet_id: str = 'cabinet-01'


class ReminderCommand(Strict):
    message: str = Field(min_length=1, max_length=500)
    due_at: str
    repeat: str = 'once'
    idempotency_key: str = Field(min_length=1, max_length=200)


class DeliveryReceipt(Strict):
    channel: str
    kind: str


def mount_routes(app, service):

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
            return await run_in_threadpool(service.query, payload, request.headers.get("x-mind-box-cabinet"))
        except VoiceError as error:
            raise HTTPException(503, str(error)) from error
        except (DatabaseError, OSError) as error:
            raise HTTPException(500, "语音服务内部错误：" + str(error)) from error

    @app.post('/api/voice/text')
    def text_command(body: TextCommand):
        try:
            return service.text_query(body.text, body.cabinet_id)
        except VoiceError as error:
            raise HTTPException(503, str(error)) from error

    @app.get('/api/voice/state')
    def voice_state():
        with service.store.db.connect() as db:
            rows = db.execute('SELECT id,origin,session_id,state,transcript,result_json,created_at '
                'FROM voice_interactions WHERE cabinet_id=? ORDER BY created_at DESC LIMIT 30', (service.cabinet_id,))
            interactions = [{**dict(row), 'result': json.loads(row['result_json'])} for row in rows]
        for item in interactions:
            del item['result_json']
        return {'interactions': interactions}

    @app.post('/api/voice/interactions/{identity}/retry')
    def retry_interaction(identity: str):
        with service.store.db.transaction() as db:
            changed = db.execute("UPDATE voice_interactions SET state='waiting_inventory',result_json='{}' "
                "WHERE id=? AND cabinet_id=? AND origin='door' AND state='failed'",
                (identity, service.cabinet_id)).rowcount
        if not changed:
            raise HTTPException(409, '只有失败的收纳语音可以重试')
        return {'id': identity, 'state': 'waiting_inventory'}

    @app.get('/api/reminders')
    def reminders():
        return {'reminders': service.tasks.reminders(service.cabinet_id),
                'deliveries': service.tasks.deliveries(service.cabinet_id)}

    @app.post('/api/reminders')
    def add_reminder(body: ReminderCommand):
        return service.tasks.schedule(service.cabinet_id, body.message, body.due_at,
                                      body.idempotency_key, body.repeat)

    @app.post('/api/reminders/{identity}/cancel')
    def cancel_reminder(identity: str):
        return service.tasks.cancel(identity, service.cabinet_id)

    @app.post('/api/reminders/deliveries/{identity}/ack')
    def acknowledge(identity: str, body: DeliveryReceipt):
        return service.tasks.acknowledge(identity, body.channel, body.kind, service.cabinet_id)


def create_app(assistant=None):
    from contextlib import asynccontextmanager
    service = assistant or VoiceAssistant()

    @asynccontextmanager
    async def lifespan(_app):
        if hasattr(service, 'store'):
            service.store.initialize()
        yield

    app = FastAPI(title='Mind Box voice service', lifespan=lifespan)
    mount_routes(app, service)
    return app


app = create_app()
