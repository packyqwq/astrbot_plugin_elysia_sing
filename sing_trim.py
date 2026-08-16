#!/usr/bin/env python3
"""
sing_trim.py — 唱歌音频裁剪库

链路: 唱歌音频 -> ASR 断句(阿里云 DashScope qwen-audio-3.0-asr-flash)
      -> 按标点切乐句 -> 在 target±window 内选乐句尾作为切点
      -> ffmpeg 裁剪 + 尾部淡出

设计给 AstrBot 插件直接 import 使用:
    from sing_trim import trim
    result = trim(audio_path, out_path, target=25.0, window=8.0, fade=1.5)

所有对外函数只用 logging,不 print (插件环境里 print 会污染日志)。
任何环节失败都会降级,不会抛异常打断调用方。

降级链:
    ASR 成功且窗口内能选到切点 -> method="asr_phrase"
    ASR 失败 或 窗口内无乐句       -> method="energy_valley" (RMS 能量谷底法)
    能量分析也失败                 -> method="hard_cut" (target 处硬切)

CLI:
    python3 sing_trim.py <输入> <输出> [--target 25] [--window 8] [--fade 1.5] [--no-asr]
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sing_trim")

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

# ASR 必须用公共端点。私有 maas 网关对这个模型返回 400 + 空 body,不能用。
ASR_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
ASR_MODEL = "qwen-audio-3.0-asr-flash"
ASR_CONFIG_PATH = Path(os.environ.get("ELYSIA_SING_ASR_CONFIG", Path(__file__).with_name("qwen_asr_config.json")))
_ASR_API_KEY = None
_FFMPEG_PATH = "ffmpeg"
_FFPROBE_PATH = "ffprobe"


def configure_asr(api_key: str):
    """Configure the DashScope key without reading outside the plugin directory."""
    global _ASR_API_KEY
    _ASR_API_KEY = (api_key or "").strip()


def configure_ffmpeg(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"):
    """Configure ffmpeg and ffprobe executable paths."""
    global _FFMPEG_PATH, _FFPROBE_PATH
    _FFMPEG_PATH = str(ffmpeg_path or "ffmpeg").strip() or "ffmpeg"
    _FFPROBE_PATH = str(ffprobe_path or "ffprobe").strip() or "ffprobe"


# 乐句切分标点: 强标点(段落级收尾) + 弱标点(短句停顿)
STRONG_PUNCT = {"。", "！", "!", "？", "?"}
WEAK_PUNCT = {"，", ",", "、", "；", ";"}
PHRASE_PUNCT = STRONG_PUNCT | WEAK_PUNCT

NEG_INF_DB = -91.0  # ffmpeg astats 对静音报的近似下限,用作"极低能量"占位值

_EXT_TO_FORMAT = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".m4a": "m4a",
    ".aac": "aac",
    ".flac": "flac",
    ".ogg": "ogg",
    ".opus": "opus",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _run(cmd: list) -> tuple[int, str, str]:
    """跑一个子进程命令,返回 (returncode, stdout, stderr)。不抛异常。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:  # noqa: BLE001 - 任何子进程异常都要吞掉,转为失败返回
        logger.warning("sing_trim: 子进程执行异常 cmd=%s err=%r", cmd, e)
        return -1, "", str(e)


def probe_duration(path: str) -> Optional[float]:
    """用 ffprobe 读音频总时长(秒)。失败返回 None。"""
    cmd = [
        _FFPROBE_PATH, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        logger.warning("sing_trim: ffprobe 读取时长失败 path=%s err=%s", path, err.strip())
        return None
    try:
        return float(out.strip())
    except ValueError:
        logger.warning("sing_trim: ffprobe 时长输出无法解析 path=%s out=%r", path, out)
        return None


def probe_format(path: str) -> tuple[Optional[int], Optional[int]]:
    """用 ffprobe 读取采样率和声道数,失败返回 (None, None)。"""
    cmd = [
        _FFPROBE_PATH, "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels",
        "-of", "csv=p=0",
        str(path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        logger.warning("sing_trim: ffprobe 读取音频格式失败 path=%s err=%s", path, err.strip())
        return None, None
    try:
        parts = out.strip().split(",")
        sample_rate = int(parts[0])
        channels = int(parts[1])
        return sample_rate, channels
    except (ValueError, IndexError):
        logger.warning("sing_trim: ffprobe 格式输出无法解析 path=%s out=%r", path, out)
        return None, None


def _guess_format(audio_path: str) -> str:
    ext = Path(audio_path).suffix.lower()
    return _EXT_TO_FORMAT.get(ext, "mp3")


# ---------------------------------------------------------------------------
# 1. ASR
# ---------------------------------------------------------------------------

def transcribe(audio_path: str, tries: int = 4) -> tuple[Optional[dict], int, Optional[str]]:
    """
    调阿里云 DashScope ASR,返回 (sentence, tries_used, last_err)。
    sentence 含 begin_time/end_time/text/words,失败(含重试耗尽)时为 None。
    tries_used 是实际尝试的次数(哪怕每次都在读 key/发请求阶段就抛异常也会计入)。
    last_err 是最后一次失败的简短原因摘要,成功时为 None。

    已知坑:
    - 必须用公共端点 ASR_ENDPOINT,私有网关对这个模型不可用。
    - format 必须放在 parameters 层,不能放 asr_options 或 content 里。
    - 接口有间歇性失败 CLIENT_ERROR: ASR_RESPONSE_HAVE_NO_WORDS,需要重试。
    """
    try:
        import httpx
    except ImportError:
        logger.warning("sing_trim: 未安装 httpx,无法调用 ASR")
        return None, 0, "ImportError: httpx 未安装"

    if not _ASR_API_KEY and not ASR_CONFIG_PATH.exists():
        logger.warning("sing_trim: ASR 配置文件不存在 path=%s", ASR_CONFIG_PATH)
        return None, 0, f"配置文件不存在: {ASR_CONFIG_PATH}"

    last_err = None
    attempt = 0
    for attempt in range(1, tries + 1):
        try:
            if _ASR_API_KEY:
                api_key = _ASR_API_KEY
            else:
                with open(ASR_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                api_key = cfg["api_key"]

            fmt = _guess_format(audio_path)
            raw = Path(audio_path).read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
            data_uri = f"data:audio/{fmt};base64,{b64}"

            payload = {
                "model": ASR_MODEL,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"audio": data_uri}],
                        }
                    ]
                },
                "parameters": {"format": fmt},
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            with httpx.Client(timeout=120.0) as client:
                resp = client.post(ASR_ENDPOINT, headers=headers, json=payload)
        except Exception as e:  # noqa: BLE001 - 重试循环内任何一步抛异常都算这次尝试失败
            last_err = f"{type(e).__name__}: {e}"[:300]
            logger.warning("sing_trim: ASR 请求异常 attempt=%d/%d err=%s", attempt, tries, last_err)
            if attempt < tries:
                time.sleep(2)
            continue

        if resp.status_code != 200:
            last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            logger.warning("sing_trim: ASR 返回非200 attempt=%d/%d %s", attempt, tries, last_err)
            if attempt < tries:
                time.sleep(2)
            continue

        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = f"JSON 解析失败: {e!r}"
            logger.warning("sing_trim: ASR 响应非JSON attempt=%d/%d err=%s", attempt, tries, last_err)
            if attempt < tries:
                time.sleep(2)
            continue

        try:
            sentence = data["output"]["output"]["sentence"]
        except (KeyError, TypeError):
            last_err = f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:300]}"
            logger.warning("sing_trim: ASR 响应缺少 sentence 字段 attempt=%d/%d %s", attempt, tries, last_err)
            if attempt < tries:
                time.sleep(2)
            continue

        words = sentence.get("words")
        if not words:
            last_err = "ASR_RESPONSE_HAVE_NO_WORDS (间歇性已知问题)"
            logger.warning("sing_trim: ASR 响应无 words attempt=%d/%d", attempt, tries)
            if attempt < tries:
                time.sleep(2)
            continue

        logger.info("sing_trim: ASR 成功 attempt=%d/%d words=%d", attempt, tries, len(words))
        sentence["_asr_tries"] = attempt
        return sentence, attempt, None

    logger.warning("sing_trim: ASR 重试耗尽 tries=%d 最后错误=%s", tries, last_err)
    return None, attempt, last_err


# ---------------------------------------------------------------------------
# 2. 按标点切乐句
# ---------------------------------------------------------------------------

def split_phrases(sentence: dict) -> list:
    """
    按 words 里的 punctuation 字段把整句切成乐句列表。
    每项: {"start": 秒, "end": 秒, "text": str, "punct": str, "is_strong": bool}
    末尾没有标点收尾的残余词也会成一句 (punct="", is_strong=False)。
    """
    words = sentence.get("words") or []
    phrases = []
    cur_words = []

    def flush(end_time_ms: int, punct: str):
        if not cur_words:
            return
        start_s = cur_words[0]["begin_time"] / 1000.0
        end_s = end_time_ms / 1000.0
        text = "".join(w.get("text", "") for w in cur_words) + punct
        is_strong = punct in STRONG_PUNCT
        phrases.append({
            "start": start_s,
            "end": end_s,
            "text": text,
            "punct": punct,
            "is_strong": is_strong,
        })

    for w in words:
        cur_words.append(w)
        punct = w.get("punctuation", "") or ""
        if punct in PHRASE_PUNCT:
            flush(w["end_time"], punct)
            cur_words = []

    # 末尾残余(没有标点收尾)也成一句
    if cur_words:
        flush(cur_words[-1]["end_time"], "")

    return phrases


# ---------------------------------------------------------------------------
# 3. 选切点
# ---------------------------------------------------------------------------

def pick_cut(phrases: list, target: float = 25.0, window: float = 8.0,
             prefer_strong: bool = True) -> Optional[float]:
    """
    在 target±window 范围内选一个乐句的 end 作为切点。
    优先 is_strong=True 的乐句尾,同类型内选最接近 target 的。
    窗口内一个都没有则返回 None。
    """
    lo, hi = target - window, target + window
    candidates = [p for p in phrases if lo <= p["end"] <= hi]
    if not candidates:
        return None

    if prefer_strong:
        strong = [p for p in candidates if p["is_strong"]]
        pool = strong if strong else candidates
    else:
        pool = candidates

    best = min(pool, key=lambda p: abs(p["end"] - target))
    return best["end"]


# ---------------------------------------------------------------------------
# 能量谷底法 (ASR 失败时的降级方案, 逻辑复用自 trim_output.py)
# ---------------------------------------------------------------------------

def _parse_astats_frames(stderr_text: str) -> list:
    """解析 ffmpeg astats/ametadata 输出,提取 (pts_time, rms_db) 列表。"""
    frames = []
    pending_time = None
    for line in stderr_text.splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            parts = line.split()
            for p in parts:
                if p.startswith("pts_time:"):
                    try:
                        pending_time = float(p.split(":", 1)[1])
                    except ValueError:
                        pending_time = None
        elif line.startswith("lavfi.astats.Overall.RMS_level="):
            if pending_time is None:
                continue
            val_str = line.split("=", 1)[1].strip()
            if val_str in ("-inf", "-nan", "nan"):
                rms = NEG_INF_DB
            else:
                try:
                    rms = float(val_str)
                except ValueError:
                    rms = NEG_INF_DB
            frames.append((pending_time, rms))
            pending_time = None
    return frames


def find_valley_cutpoint(input_path: str, target: float, window: float,
                          total_duration: float) -> Optional[tuple]:
    """
    在 [target-window, target+window] (clamp 到 [0, total_duration]) 内
    用 0.1s RMS 窗口找能量谷底,返回 (cut_point, valley_db, peak_db)。
    若分析失败返回 None。
    """
    win_start = max(0.0, target - window)
    win_end = min(total_duration, target + window)
    if win_end <= win_start:
        return None

    sample_rate, _channels = probe_format(input_path)
    if not sample_rate:
        sample_rate = 24000  # 兜底

    n_samples = max(1, round(sample_rate * 0.1))  # 0.1s 窗口对应的采样点数

    filt = (
        f"asetnsamples=n={n_samples},"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )

    cmd = [
        _FFMPEG_PATH, "-v", "info",
        "-ss", f"{win_start:.6f}",
        "-t", f"{(win_end - win_start):.6f}",
        "-i", str(input_path),
        "-af", filt,
        "-f", "null", "-",
    ]
    try:
        rc, out, err = _run(cmd)
        if rc != 0:
            logger.warning("sing_trim: 能量分析 ffmpeg 失败 err=%s", err[-500:])
            return None

        # ametadata 的 file=- 写的是子进程 stdout,不是 stderr
        frames = _parse_astats_frames(out)
        if not frames:
            logger.warning("sing_trim: 能量分析未解析到任何帧")
            return None

        # frame 的 pts_time 是相对裁剪窗口起点的偏移,换算回原始时间轴
        abs_frames = [(win_start + t, rms) for t, rms in frames]
        valley_time, valley_db = min(abs_frames, key=lambda x: x[1])
        peak_db = max(rms for _t, rms in abs_frames)
        return valley_time, valley_db, peak_db
    except Exception as e:  # noqa: BLE001 - 能量分析任何异常都要降级为 None,不抛给调用方
        logger.warning("sing_trim: 能量分析异常 err=%r", e)
        return None


# ---------------------------------------------------------------------------
# ffmpeg 裁剪 + 淡出
# ---------------------------------------------------------------------------

def _cut_with_fade(input_path: str, output_path: str, cut_point: float, fade: float,
                    sample_rate: Optional[int], channels: Optional[int]) -> tuple:
    """裁剪到 cut_point 并在尾部加淡出。返回 (ok, err, effective_fade)。"""
    effective_fade = fade
    if cut_point - fade < 0:
        effective_fade = max(0.0, cut_point)
        logger.warning(
            "sing_trim: 切点太靠前,fade 从 %.2fs 缩短为 %.2fs (cut_point=%.2f)",
            fade, effective_fade, cut_point,
        )

    af_filters = []
    if effective_fade > 0:
        fade_start = max(0.0, cut_point - effective_fade)
        af_filters.append(f"afade=t=out:st={fade_start:.6f}:d={effective_fade:.6f}")

    cmd = [_FFMPEG_PATH, "-y", "-v", "error", "-i", str(input_path), "-t", f"{cut_point:.6f}"]
    if af_filters:
        cmd += ["-af", ",".join(af_filters)]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += ["-b:a", "192k", str(output_path)]

    rc, _out, err = _run(cmd)
    return rc == 0, err, effective_fade


def _copy_transcode(input_path: str, output_path: str,
                     sample_rate: Optional[int], channels: Optional[int]) -> tuple:
    """不裁剪场景:直接转码到目标格式(mp3 192k,保持采样率/声道)。"""
    cmd = [_FFMPEG_PATH, "-y", "-v", "error", "-i", str(input_path)]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += ["-b:a", "192k", str(output_path)]
    rc, _out, err = _run(cmd)
    return rc == 0, err


# ---------------------------------------------------------------------------
# 4. 主函数
# ---------------------------------------------------------------------------

def trim(audio_path: str, out_path: str, target: float = 25.0, window: float = 8.0,
          fade: float = 1.5, asr_tries: int = 4, use_asr: bool = True) -> dict:
    """
    唱歌音频裁剪主函数。不抛异常,任何环节失败都会降级并在返回值 method 字段体现。

    返回结构示例:
    {
      "trimmed": True,
      "orig_dur": 50.04,
      "cut_at": 25.60,
      "out_dur": 25.6,
      "method": "asr_phrase",
      "phrase_text": "一尘不染的真心。",
      "phrases_total": 12,
      "asr_tries": 2,
    }
    """
    result = {
        "trimmed": False,
        "orig_dur": None,
        "cut_at": None,
        "out_dur": None,
        "method": None,
        "phrase_text": None,
        "phrases_total": 0,
        "asr_tries": 0,
        "error": None,
    }

    orig_dur = probe_duration(audio_path)
    if orig_dur is None:
        result["error"] = "无法读取原始音频时长(ffprobe 失败)"
        logger.warning("sing_trim: %s path=%s", result["error"], audio_path)
        return result
    result["orig_dur"] = orig_dur

    sample_rate, channels = probe_format(audio_path)

    # 1) 不需要裁剪
    if orig_dur <= target + 3:
        ok, err = _copy_transcode(audio_path, out_path, sample_rate, channels)
        if not ok:
            result["error"] = f"转码复制失败: {err[-300:]}"
            logger.warning("sing_trim: %s", result["error"])
            return result
        result["method"] = "no_trim"
        result["out_dur"] = probe_duration(out_path)
        logger.info("sing_trim: 原时长 %.2fs <= target+3, 无需裁剪", orig_dur)
        return result

    cut_point = None
    method = None
    phrase_text = None
    phrases_total = 0
    tries_used = 0
    degrade_reason = None  # 降级发生时记录简短原因,最终写入 result["error"]

    # 2) ASR + 按乐句切点
    if use_asr:
        sentence, tries_used, asr_err = transcribe(audio_path, tries=asr_tries)
        if sentence is not None:
            phrases = split_phrases(sentence)
            phrases_total = len(phrases)
            cp = pick_cut(phrases, target=target, window=window, prefer_strong=True)
            if cp is not None:
                cut_point = cp
                method = "asr_phrase"
                for p in phrases:
                    if abs(p["end"] - cp) < 1e-6:
                        phrase_text = p["text"]
                        break
            else:
                degrade_reason = f"asr_no_phrase_in_window: 窗口 [{target - window:.1f}, {target + window:.1f}] 内无乐句"
                logger.warning(
                    "sing_trim: ASR 成功但窗口 [%.2f, %.2f] 内无乐句可选,降级到能量谷底法",
                    target - window, target + window,
                )
        else:
            degrade_reason = f"asr_failed: {asr_err} after {tries_used} tries"
            logger.warning("sing_trim: ASR 失败,降级到能量谷底法 err=%s", asr_err)
    else:
        logger.info("sing_trim: use_asr=False,跳过 ASR 直接用能量谷底法")

    # 3) 降级: 能量谷底法
    if cut_point is None:
        valley = find_valley_cutpoint(audio_path, target, window, orig_dur)
        if valley is not None:
            cut_point, _valley_db, _peak_db = valley
            method = "energy_valley"
        else:
            # 4) 降级: 硬切
            if degrade_reason is None:
                degrade_reason = "energy_valley_failed: 能量分析未取得有效数据"
            else:
                degrade_reason = f"{degrade_reason}; energy_valley_failed"
            logger.warning("sing_trim: 能量分析也失败,降级为 target 处硬切")
            cut_point = min(target, orig_dur)
            method = "hard_cut"

    if method != "asr_phrase":
        result["error"] = degrade_reason

    cut_point = max(0.0, min(cut_point, orig_dur))

    ok, err, _effective_fade = _cut_with_fade(audio_path, out_path, cut_point, fade,
                                                sample_rate, channels)
    if not ok:
        result["error"] = f"ffmpeg 裁剪失败: {err[-300:]}"
        logger.warning("sing_trim: %s", result["error"])
        return result

    result["trimmed"] = True
    result["cut_at"] = round(cut_point, 2)
    result["out_dur"] = probe_duration(out_path)
    result["method"] = method
    result["phrase_text"] = phrase_text
    result["phrases_total"] = phrases_total
    result["asr_tries"] = tries_used

    logger.info(
        "sing_trim: 完成 method=%s cut_at=%.2f out_dur=%s asr_tries=%d",
        method, cut_point, result["out_dur"], tries_used,
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main():
    parser = argparse.ArgumentParser(description="唱歌音频裁剪: ASR 断句 + 按乐句切点 + 淡出")
    parser.add_argument("input", help="输入音频文件")
    parser.add_argument("output", help="输出音频文件")
    parser.add_argument("--target", type=float, default=25.0, help="目标时长(秒),默认 25")
    parser.add_argument("--window", type=float, default=8.0, help="切点搜索窗口 ± 秒,默认 8")
    parser.add_argument("--fade", type=float, default=1.5, help="淡出时长(秒),默认 1.5")
    parser.add_argument("--asr-tries", type=int, default=4, help="ASR 重试次数,默认 4")
    parser.add_argument("--no-asr", action="store_true", help="跳过 ASR,直接用能量谷底法")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = trim(
        args.input, args.output,
        target=args.target, window=args.window, fade=args.fade,
        asr_tries=args.asr_tries, use_asr=not args.no_asr,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
