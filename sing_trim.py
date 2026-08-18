#!/usr/bin/env python3
"""火山 SAUC ASR 与音频乐句裁剪。"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import math
import re
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sing_trim")
ASR_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
ASR_RESOURCE_ID = "volc.seedasr.sauc.duration"
_ASR_API_KEY = None
_FFMPEG_PATH = "ffmpeg"
_FFPROBE_PATH = "ffprobe"
STRONG_PUNCT = {"。", "！", "!", "？", "?"}
NEG_INF_DB = -91.0


def configure_asr(api_key: str):
    global _ASR_API_KEY
    _ASR_API_KEY = (api_key or "").strip()


def configure_ffmpeg(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"):
    global _FFMPEG_PATH, _FFPROBE_PATH
    _FFMPEG_PATH = str(ffmpeg_path or "ffmpeg").strip() or "ffmpeg"
    _FFPROBE_PATH = str(ffprobe_path or "ffprobe").strip() or "ffprobe"


def _run(cmd: list) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        logger.warning("sing_trim: 子进程执行异常 cmd=%s err=%r", cmd, exc)
        return -1, "", str(exc)


def probe_duration(path: str) -> Optional[float]:
    rc, out, err = _run([_FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    if rc != 0:
        logger.warning("sing_trim: ffprobe 读取时长失败 path=%s err=%s", path, err.strip())
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def probe_format(path: str) -> tuple[Optional[int], Optional[int]]:
    rc, out, err = _run([_FFPROBE_PATH, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(path)])
    if rc != 0:
        logger.warning("sing_trim: ffprobe 读取格式失败 path=%s err=%s", path, err.strip())
        return None, None
    try:
        rate, channels = out.strip().split(",")
        return int(rate), int(channels)
    except (ValueError, IndexError):
        return None, None


def _asr_frame(message_type: int, flags: int, payload: bytes, sequence: int) -> bytes:
    payload = gzip.compress(payload)
    return bytes((0x11, (message_type << 4) | flags, 0x11, 0)) + struct.pack(">i", sequence) + struct.pack(">I", len(payload)) + payload


def _parse_asr_frame(frame: bytes) -> dict:
    max_payload = 10 * 1024 * 1024
    max_decompressed = 50 * 1024 * 1024
    if len(frame) < 4 or frame[0] != 0x11 or (frame[0] & 0x0F) != 1:
        raise ValueError("invalid ASR frame header")
    message_type, flags, offset = frame[1] >> 4, frame[1] & 0x0F, 4
    sequence = None
    if flags & 1:
        if offset + 4 > len(frame):
            raise ValueError("truncated ASR sequence")
        sequence = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4
    error_code = None
    if message_type == 0xF:
        if offset + 4 > len(frame):
            raise ValueError("truncated ASR error code")
        error_code = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4
    if offset + 4 > len(frame):
        raise ValueError("missing ASR payload size")
    size = struct.unpack(">I", frame[offset:offset + 4])[0]
    if size > max_payload or size != len(frame) - offset - 4:
        raise ValueError("invalid ASR payload size")
    payload = frame[offset + 4:offset + 4 + size]
    if (frame[2] & 0x0F) == 1:
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            payload = stream.read(max_decompressed + 1)
        if len(payload) > max_decompressed:
            raise ValueError("ASR decompressed payload too large")
    return {"message_type": message_type, "sequence": sequence, "error_code": error_code,
            "is_last_package": bool(flags & 2), "payload": json.loads(payload) if payload else {}}


async def _transcribe_pcm(api_key: str, pcm: bytes) -> dict:
    import websockets
    headers = {"X-Api-Key": api_key, "X-Api-Resource-Id": ASR_RESOURCE_ID,
               "X-Api-Request-Id": str(uuid.uuid4()), "X-Api-Sequence": "-1"}
    request = {"user": {"uid": "elysia_sing"}, "audio": {"format": "pcm", "rate": 16000, "bits": 16, "channel": 1},
               "request": {"model_name": "bigmodel", "show_utterances": True, "enable_punc": True}}
    async with websockets.connect(ASR_ENDPOINT, additional_headers=headers, open_timeout=20, close_timeout=10) as ws:
        await ws.send(_asr_frame(1, 1, json.dumps(request, ensure_ascii=False).encode(), 1))
        sequence = 2
        for offset in range(0, len(pcm), 3200):
            chunk = pcm[offset:offset + 3200]
            last = offset + len(chunk) == len(pcm)
            await ws.send(_asr_frame(2, 3 if last else 1, chunk, -sequence if last else sequence))
            sequence += 1
        while True:
            reply = await asyncio.wait_for(ws.recv(), timeout=45)
            if isinstance(reply, str):
                continue
            parsed = _parse_asr_frame(reply)
            if parsed["message_type"] == 0xF:
                raise RuntimeError(f"Volc ASR error {parsed['error_code']}: {parsed['payload']}")
            if parsed["is_last_package"]:
                return parsed["payload"]


def transcribe(audio_path: str, tries: int = 4) -> tuple[Optional[dict], int, Optional[str]]:
    """同步 ASR 入口；只能在线程中调用，不得从正在运行的事件循环线程调用。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("transcribe() must be called from a worker thread")
    if not _ASR_API_KEY:
        return None, 0, "未配置火山引擎 API Key"
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            input_args = [_FFMPEG_PATH, "-v", "error"]
            if Path(audio_path).suffix.lower() == ".pcm":
                input_args += ["-f", "s16le", "-ar", "16000", "-ac", "1"]
            input_args += ["-i", str(audio_path), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"]
            proc = subprocess.run(input_args, capture_output=True, timeout=120)
            if proc.returncode:
                raise RuntimeError(proc.stderr.decode(errors="replace")[-300:])
            result = asyncio.run(_transcribe_pcm(_ASR_API_KEY, proc.stdout))
            utterances = (result.get("result") or {}).get("utterances") or []
            if not utterances:
                raise RuntimeError("ASR response has no utterances")
            return result, attempt, None
        except Exception as exc:
            last_err = re.sub(r"[A-Za-z0-9]{20,}", "[REDACTED]", f"{type(exc).__name__}: {exc}")[:300]
            logger.warning("sing_trim: ASR 失败 attempt=%d/%d err=%s", attempt, tries, last_err)
            if attempt < tries:
                time.sleep(2)
    return None, tries, last_err


def split_phrases(sentence: dict) -> list:
    phrases = []
    utterances = (sentence.get("result") or {}).get("utterances") or []
    for utterance in utterances:
        text = str(utterance.get("text") or "")
        if not text:
            continue
        stripped_text = text.rstrip()
        punct = stripped_text[-1] if stripped_text and stripped_text[-1] in STRONG_PUNCT else ""
        try:
            start = float(utterance["start_time"]) / 1000.0
            end = float(utterance["end_time"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            logger.warning("split_phrases skip: reason=missing_or_bad_time_keys keys=%s "
                            "start_time_raw=%r end_time_raw=%r",
                            sorted(utterance.keys()),
                            utterance.get("start_time"), utterance.get("end_time"))
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end < start:
            logger.info("split_phrases skip: reason=invalid_time_range keys=%s "
                        "start_time_raw=%r end_time_raw=%r",
                        sorted(utterance.keys()),
                        utterance.get("start_time"), utterance.get("end_time"))
            continue
        phrases.append({"start": start, "end": end, "text": text, "punct": punct, "is_strong": bool(punct)})
    return phrases


def pick_cut(phrases: list, target: float = 25.0, window: float = 8.0, prefer_strong: bool = True) -> Optional[float]:
    candidates = [p for p in phrases if target - window <= p["end"] <= target + window]
    if not candidates:
        return None
    strong = [p for p in candidates if p["is_strong"]]
    return min(strong if prefer_strong and strong else candidates, key=lambda p: abs(p["end"] - target))["end"]


def find_valley_cutpoint(input_path: str, target: float, window: float, total_duration: float) -> Optional[tuple]:
    win_start, win_end = max(0.0, target - window), min(total_duration, target + window)
    rate, _ = probe_format(input_path)
    rate = rate or 24000
    filt = f"asetnsamples=n={max(1, round(rate * 0.1))},astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    rc, out, err = _run([_FFMPEG_PATH, "-v", "info", "-ss", f"{win_start:.6f}", "-t", f"{win_end - win_start:.6f}", "-i", str(input_path), "-af", filt, "-f", "null", "-"])
    if rc != 0:
        return None
    frames = []
    for line in out.splitlines():
        if "lavfi.astats.Overall.RMS_level=" in line:
            try:
                frames.append((float(line.split("pts_time:", 1)[1].split()[0]) + win_start, float(line.rsplit("=", 1)[1])))
            except (ValueError, IndexError):
                pass
    if not frames:
        return None
    valley = min(frames, key=lambda item: item[1])
    peak = max(item[1] for item in frames)
    return valley[0], valley[1], peak


def _cut_with_fade(input_path: str, output_path: str, cut_point: float, fade: float, sample_rate: Optional[int], channels: Optional[int]) -> tuple:
    effective_fade = min(max(0.0, fade), max(0.0, cut_point))
    cmd = [_FFMPEG_PATH, "-y", "-v", "error", "-i", str(input_path), "-t", f"{cut_point:.6f}"]
    if effective_fade > 0:
        cmd += ["-af", f"afade=t=out:st={max(0.0, cut_point-effective_fade):.6f}:d={effective_fade:.6f}"]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += ["-c:a", "pcm_s16le", str(output_path)]
    rc, _, err = _run(cmd)
    return rc == 0, err, effective_fade


def _copy_transcode(input_path: str, output_path: str, sample_rate: Optional[int], channels: Optional[int]) -> tuple:
    cmd = [_FFMPEG_PATH, "-y", "-v", "error", "-i", str(input_path)]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += ["-c:a", "pcm_s16le", str(output_path)]
    rc, _, err = _run(cmd)
    return rc == 0, err


def trim(audio_path: str, out_path: str, target: float = 25.0, window: float = 8.0, fade: float = 1.5, asr_tries: int = 4, use_asr: bool = True, asr_result: Optional[dict] = None) -> dict:
    result = {"trimmed": False, "orig_dur": None, "cut_at": None, "out_dur": None, "method": None, "phrase_text": None, "phrases_total": 0, "asr_tries": 0, "error": None}
    orig_dur = probe_duration(audio_path)
    if orig_dur is None:
        result["error"] = "无法读取原始音频时长(ffprobe 失败)"
        return result
    result["orig_dur"] = orig_dur
    sample_rate, channels = probe_format(audio_path)
    if orig_dur <= target + 3:
        ok, err = _copy_transcode(audio_path, out_path, sample_rate, channels)
        if not ok:
            result["error"] = f"转码复制失败: {err[-300:]}"
            return result
        result.update(method="no_trim", out_dur=probe_duration(out_path))
        return result
    cut_point = None
    method = None
    phrase_text = None
    phrases_total = 0
    tries_used = 0
    degrade_reason = None
    if use_asr:
        sentence, tries_used, asr_err = (asr_result, 0, None) if asr_result is not None else transcribe(audio_path, tries=asr_tries)
        if sentence is not None:
            phrases = split_phrases(sentence)
            phrases_total = len(phrases)
            cp = pick_cut(phrases, target, window, True)
            if cp is not None:
                cut_point, method = cp, "asr_phrase"
                phrase_text = next((p["text"] for p in phrases if abs(p["end"] - cp) < 1e-6), None)
            else:
                degrade_reason = "asr_no_phrase_in_window"
        else:
            degrade_reason = f"asr_failed: {asr_err}"
    if cut_point is None:
        valley = find_valley_cutpoint(audio_path, target, window, orig_dur)
        if valley is not None:
            cut_point, method = valley[0], "energy_valley"
        else:
            cut_point, method = min(target, orig_dur), "hard_cut"
            degrade_reason = (degrade_reason + "; energy_valley_failed") if degrade_reason else "energy_valley_failed"
    ok, err, _ = _cut_with_fade(audio_path, out_path, cut_point, fade, sample_rate, channels)
    if not ok:
        result["error"] = f"ffmpeg 裁剪失败: {err[-300:]}"
        return result
    result.update(trimmed=True, cut_at=round(cut_point, 2), out_dur=probe_duration(out_path), method=method, phrase_text=phrase_text, phrases_total=phrases_total, asr_tries=tries_used, error=None if method == "asr_phrase" else degrade_reason)
    return result


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--target", type=float, default=25)
    parser.add_argument("--window", type=float, default=8)
    parser.add_argument("--fade", type=float, default=1.5)
    parser.add_argument("--no-asr", action="store_true")
    args = parser.parse_args()
    result = trim(args.input, args.output, args.target, args.window, args.fade, use_asr=not args.no_asr)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
