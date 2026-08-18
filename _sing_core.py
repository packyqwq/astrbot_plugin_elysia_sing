import asyncio
import base64
import difflib
import json
import logging
import shutil
import subprocess
import re
import uuid
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import WebSocketException

try:
    from . import protocol as proto
    from . import sing_trim
except ImportError:
    import protocol as proto
    import sing_trim

FRAME_BYTES = 640
FRAME_INTERVAL = 0.020
RECV_TIMEOUT = 20.0
logger = logging.getLogger(__name__)
_FFMPEG_PATH = "ffmpeg"
_FFPROBE_PATH = "ffprobe"
VOLC_APP_KEY = "PlgvMymc7f3tQnJ6"
VOLC_RESOURCE_ID = "volc.speech.dialog"
VOLC_WS_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
VOLC_TTS_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
VOLC_TTS_RESOURCE_ID = "seed-tts-2.0"


def configure_ffmpeg(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"):
    """Configure ffmpeg and ffprobe executable paths."""
    global _FFMPEG_PATH, _FFPROBE_PATH
    _FFMPEG_PATH = str(ffmpeg_path or "ffmpeg").strip() or "ffmpeg"
    _FFPROBE_PATH = str(ffprobe_path or "ffprobe").strip() or "ffprobe"
    for name, path in (("ffmpeg", _FFMPEG_PATH), ("ffprobe", _FFPROBE_PATH)):
        if not shutil.which(path):
            logger.warning("%s executable not found: %s; configure its full path", name, path)


def _cfg(config, key, default=None):
    value = config.get(key, default)
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


class _Secret:
    """Keep credentials usable without exposing them in expanded tracebacks."""

    def __init__(self, value):
        self._value = str(value or "")

    @property
    def value(self):
        return self._value

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__


class _RedactedHeaders(dict):
    """Avoid rendering credential-bearing headers in local-variable traces."""

    def __repr__(self):
        return "<redacted headers>"


async def run_ffmpeg(args, timeout=120):
    command = [_FFMPEG_PATH, "-y", *map(str, args)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except NotImplementedError:
        # SelectorEventLoop on Windows has no asyncio subprocess support.
        try:
            completed = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run, command, capture_output=True, timeout=timeout,
                ), timeout=timeout + 1,
            )
        except (subprocess.TimeoutExpired, asyncio.TimeoutError):
            raise RuntimeError("ffmpeg timeout") from None
        stderr = completed.stderr or b""
        if completed.returncode:
            raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[-500:]}")
        return
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("ffmpeg timeout")
    if proc.returncode:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[-500:]}")


async def synth_query(api_key, tts_speaker, text, out_pcm):
    """Synthesize a 16 kHz mono s16le PCM prompt for the S2S endpoint."""
    headers = _RedactedHeaders({
        "X-Api-Key": api_key.value,
        "X-Api-Resource-Id": VOLC_TTS_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    })
    body = {"req_params": {"text": text,
                            "speaker": tts_speaker,
                            "audio_params": {"format": "pcm", "sample_rate": 16000}}}
    audio = bytearray()
    completed = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            async with client.stream("POST", VOLC_TTS_ENDPOINT, headers=headers, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    content = line.removeprefix("data:").strip()
                    try:
                        packet = json.loads(content)
                    except json.JSONDecodeError:
                        if content in ("[DONE]", "done"):
                            completed = True
                            continue
                        raise RuntimeError("Volc TTS returned invalid stream packet") from None
                    code = packet.get("code")
                    if code not in (None, 0, 20000000):
                        raise RuntimeError(f"Volc TTS error: {re.sub(r'[A-Za-z0-9]{20,}', '[REDACTED]', json.dumps(packet, ensure_ascii=False))[:300]}")
                    data = packet.get("data")
                    if isinstance(data, str) and data:
                        try:
                            audio.extend(base64.b64decode(data, validate=True))
                        except (ValueError, base64.binascii.Error):
                            raise RuntimeError("Volc TTS returned invalid audio data") from None
                    if code == 20000000 or packet.get("is_end") is True or packet.get("end") is True:
                        completed = True
    except httpx.HTTPStatusError as exc:
        detail = re.sub(r"[A-Za-z0-9]{20,}", "[REDACTED]", (await exc.response.aread()).decode(errors="replace"))[:200]
        raise RuntimeError(f"Volc TTS HTTP {exc.response.status_code}: {detail}") from None
    except httpx.RequestError as exc:
        raise RuntimeError(f"Volc TTS network error: {type(exc).__name__}") from None
    if not audio or not completed:
        raise RuntimeError("Volc TTS stream completed without audio or completion marker")
    Path(out_pcm).write_bytes(audio)


async def _collect_response(ws, output_pcm, overall_timeout):
    result = {"audio_bytes": 0, "errors": [], "tts_types": [], "chat_text": []}
    deadline = asyncio.get_running_loop().time() + overall_timeout
    with open(output_pcm, "wb") as audio_file:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("S2S overall timeout")
            frame = await asyncio.wait_for(ws.recv(), timeout=min(RECV_TIMEOUT, remaining))
            parsed = proto.unpack(frame if isinstance(frame, bytes) else frame.encode())
            if parsed.event == proto.EVENT_TTS_RESPONSE or parsed.message_type == proto.MSG_TYPE_AUDIO_ONLY_RESPONSE:
                audio_file.write(parsed.payload)
                result["audio_bytes"] += len(parsed.payload)
                continue
            if parsed.event == proto.EVENT_TTS_SENTENCE_START and parsed.json:
                result["tts_types"].append(parsed.json.get("tts_type"))
            elif parsed.event == proto.EVENT_CHAT_RESPONSE and parsed.json:
                result["chat_text"].append(parsed.json.get("content", ""))
            if parsed.event in (proto.EVENT_DIALOG_COMMON_ERROR, proto.EVENT_SESSION_FAILED) or parsed.message_type == proto.MSG_TYPE_ERROR:
                result["errors"].append({"event": parsed.event, "code": parsed.error_code, "json": parsed.json})
                break
            if parsed.event == proto.EVENT_TTS_ENDED:
                break
    return result


async def _receive(ws):
    frame = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
    return proto.unpack(frame if isinstance(frame, bytes) else frame.encode())


async def _close_session(ws, session_id):
    try:
        await ws.send(proto.pack_json(proto.EVENT_FINISH_SESSION, {}, session_id=session_id))
        await _receive(ws)
    except (RuntimeError, TimeoutError, OSError, ValueError, WebSocketException):
        pass
    try:
        await ws.send(proto.pack_json(proto.EVENT_FINISH_CONNECTION, {}))
    except (RuntimeError, TimeoutError, OSError, WebSocketException):
        pass


async def s2s_once(api_key, speaker, timeout, input_pcm, output_pcm):
    audio = Path(input_pcm).read_bytes()
    if not audio:
        raise RuntimeError("query PCM is empty")
    session_id = proto.new_uuid()
    headers = _RedactedHeaders({
        "X-Api-Key": api_key.value,
        "X-Api-Resource-Id": VOLC_RESOURCE_ID,
        "X-Api-App-Key": VOLC_APP_KEY,
        "X-Api-Connect-Id": proto.new_uuid(),
    })
    endpoint = VOLC_WS_ENDPOINT
    try:
        async with websockets.connect(endpoint, additional_headers=headers, open_timeout=15) as ws:
            await ws.send(proto.pack_json(proto.EVENT_START_CONNECTION, {}))
            if (await _receive(ws)).event != proto.EVENT_CONNECTION_STARTED:
                raise RuntimeError("S2S connection not started")
            start_payload = {
                "asr": {"extra": {}, "audio_info": {"format": "pcm", "sample_rate": 16000, "channel": 1}},
                "tts": {"speaker": speaker, "extra": {},
                        "audio_config": {"channel": 1, "format": "pcm_s16le", "sample_rate": 24000}},
                "dialog": {"extra": {"input_mod": "audio_file", "enable_music": True, "model": "1.2.1.1"}},
            }
            await ws.send(proto.pack_json(proto.EVENT_START_SESSION, start_payload, session_id=session_id))
            if (await _receive(ws)).event != proto.EVENT_SESSION_STARTED:
                raise RuntimeError("S2S session not started")
            try:
                for offset in range(0, len(audio), FRAME_BYTES):
                    await ws.send(proto.pack_audio(proto.EVENT_TASK_REQUEST, audio[offset:offset + FRAME_BYTES], session_id))
                    await asyncio.sleep(FRAME_INTERVAL)
                return await _collect_response(ws, output_pcm, timeout)
            finally:
                await _close_session(ws, session_id)
    except WebSocketException as exc:
        raise RuntimeError(f"Volc S2S connection error: {type(exc).__name__}") from None


async def s2s_with_retries(api_key, speaker, timeout, retries, input_pcm, output_pcm):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            result = await s2s_once(api_key, speaker, timeout, input_pcm, output_pcm)
            if result["audio_bytes"] <= 0 or result["errors"]:
                raise RuntimeError(f"S2S returned no valid audio: {result['errors']}")
            return result
        except (RuntimeError, TimeoutError, OSError, ValueError, WebSocketException) as exc:
            last_error = exc
            Path(output_pcm).unlink(missing_ok=True)
            if attempt < retries:
                await asyncio.sleep(2)
    raise RuntimeError(f"S2S failed after {retries} attempts: {last_error}")


def _normalized(text):
    return "".join(re.findall(r"[一-鿿A-Za-z0-9]", text or "")).lower()


def best_lyric_start(phrases, hint_lyrics):
    hint = _normalized(hint_lyrics)
    if not hint or not phrases:
        return None, 0.0
    best = (None, 0.0)
    for i in range(len(phrases)):
        for size in range(1, 4):
            window = phrases[i:i + size]
            if len(window) != size:
                continue
            candidate = _normalized("".join(str(p.get("text", "")) for p in window))
            score = difflib.SequenceMatcher(None, hint, candidate).ratio() if candidate else 0.0
            if score > best[1]:
                best = (float(window[0]["start"]), score)
    return best


def validate_output(song, hint_lyrics, sentence, duration, tts_types=None):
    text = "".join(str(item.get("text", "")) for item in ((sentence or {}).get("result") or {}).get("utterances", []))
    refusal_terms = ("我不会唱", "还不会唱", "换一首", "唱一首别的", "不能唱", "无法唱")
    if any(term in text for term in refusal_terms):
        return False, "engine_refused_to_sing"
    if duration is None or duration < 6.0:
        return False, f"生成音频过短（{duration or 0:.2f} 秒）"
    if hint_lyrics:
        expected = set(re.findall(r"[一-鿿]", hint_lyrics))
        actual = set(re.findall(r"[一-鿿]", text))
        if expected and len(expected & actual) / len(expected) < 0.15:
            return False, "识别内容与提示歌词重合比例过低"
    if tts_types is not None and "sing" not in tts_types:
        return True, "S2S 响应未包含 sing 类型，音频时长正常，放行"
    return True, ""


async def trim_output(target, window, fade_in, fade_out, input_mp3, output_wav, sentence, hint_lyrics, asr_attempted=False):
    phrases = sing_trim.split_phrases(sentence) if sentence else []
    start, score = best_lyric_start(phrases, hint_lyrics)
    if start is not None and score >= 0.35:
        probed_duration = await asyncio.to_thread(sing_trim.probe_duration, input_mp3)
        if probed_duration is None:
            result = await asyncio.to_thread(sing_trim.trim, input_mp3, output_wav, target, window, fade_out, 4, not asr_attempted, sentence)
            if not Path(output_wav).exists():
                raise RuntimeError(f"trim failed: {result.get('error')}")
            return result
        duration = min(target, max(0.1, probed_duration - start))
        filters = [f"afade=t=in:st=0:d={min(fade_in, duration):.3f}"]
        if fade_out > 0:
            filters.append(f"afade=t=out:st={max(0, duration - fade_out):.3f}:d={min(fade_out, duration):.3f}")
        await run_ffmpeg(["-ss", f"{start:.3f}", "-i", input_mp3, "-t", f"{duration:.3f}",
                          "-af", ",".join(filters), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", output_wav])
        return {"method": "lyrics_match", "start": start, "score": score, "out_dur": duration}
    result = await asyncio.to_thread(sing_trim.trim, input_mp3, output_wav, target, window, fade_out, 4, not asr_attempted, sentence)
    if not Path(output_wav).exists():
        raise RuntimeError(f"trim failed: {result.get('error')}")
    return result


async def execute_pipeline(config, job_dir, song, request, hint_lyrics):
    api_key = _Secret(_cfg(config, "volc_api_key", ""))
    speaker = str(_cfg(config, "volc_speaker", ""))
    tts_speaker = str(_cfg(config, "volc_tts_speaker", "zh_female_vv_uranus_bigtts"))
    s2s_timeout = float(_cfg(config, "s2s_timeout", 90.0))
    s2s_retries = max(1, int(_cfg(config, "s2s_retries", 5)))
    target = float(_cfg(config, "target", 25.0))
    window = float(_cfg(config, "window", 8.0))
    fade_in = float(_cfg(config, "fade_in", 0.5))
    fade_out = float(_cfg(config, "fade_out", 1.5))
    ffmpeg_path = _cfg(config, "ffmpeg_path", "ffmpeg")
    ffprobe_path = _cfg(config, "ffprobe_path", "ffprobe")
    del config
    if not speaker:
        raise RuntimeError("未配置火山音色编号,请在插件设置里填写")
    configure_ffmpeg(ffmpeg_path, ffprobe_path)
    sing_trim.configure_ffmpeg(ffmpeg_path, ffprobe_path)
    job_dir = Path(job_dir)
    query_pcm = job_dir / "tmp_query.pcm"
    output_pcm = job_dir / "tmp_sing.pcm"
    output_mp3 = job_dir / "tmp_sing.mp3"
    final_wav = job_dir / "final.wav"
    await synth_query(api_key, tts_speaker, request or f"请唱{song}", query_pcm)
    s2s_result = await s2s_with_retries(api_key, speaker, s2s_timeout, s2s_retries, query_pcm, output_pcm)
    await run_ffmpeg(["-f", "s16le", "-ar", "24000", "-ac", "1", "-i", output_pcm,
                      "-b:a", "192k", output_mp3])
    sing_trim.configure_asr(api_key.value)
    sentence, tries, asr_error = await asyncio.to_thread(sing_trim.transcribe, str(output_mp3), 4)
    duration = await asyncio.to_thread(sing_trim.probe_duration, str(output_mp3))
    valid, reason = validate_output(song, hint_lyrics, sentence, duration, s2s_result.get("tts_types"))
    if not valid:
        raise RuntimeError(reason)
    if reason:
        logger.warning("[elysia_sing] %s", reason)
    trim_result = await trim_output(target, window, fade_in, fade_out, str(output_mp3), str(final_wav), sentence, hint_lyrics, asr_attempted=True)
    return final_wav, {"s2s": s2s_result, "asr_tries": tries, "asr_error": asr_error,
                       "duration": duration, "trim": trim_result}
