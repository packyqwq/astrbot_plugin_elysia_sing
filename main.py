import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from . import _sing_core

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    def get_astrbot_data_path() -> str:
        return str(Path.cwd() / "data")

PLUGIN_NAME = "astrbot_plugin_elysia_sing"
DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


def _num(config, key, default, cast):
    try:
        return cast(config.get(key, default))
    except (TypeError, ValueError):
        return default


# ─── 主动通知发送前的标记清理（仅剥离明确的标记形式，不动正常中文括号）───
# elysiatts 的情绪标签 [[emo:xxx]] 或 [[emo:xxx|语速]]
_NOTIFY_EMO_TAG = re.compile(r"\[\[\s*emo\s*:[^\[\]]*\]\]", re.IGNORECASE)
# elysiatts 的停顿标签 <#秒数#>
_NOTIFY_PAUSE_TAG = re.compile(r"<#\s*[0-9]+(?:\.[0-9]+)?\s*#>")
# 纯英文单词/短拟声词构成的圆括号内容，如 (chuckle)(sigh)，中文括号内容不受影响
_NOTIFY_EN_PAREN = re.compile(r"\(\s*[a-zA-Z][a-zA-Z\s\-']*\)")
# 纯英文单词/短拟声词构成的星号内容，如 *smile*
_NOTIFY_EN_ASTERISK = re.compile(r"\*\s*[a-zA-Z][a-zA-Z\s\-']*\*")
# 清理后残留的多余空白
_NOTIFY_EXTRA_WS = re.compile(r"[ \t]{2,}")


def _clean_notify_text(text: str) -> str:
    """清理主动通知文本里绕过 elysiatts decorating 链而裸露的内部标记。
    只剥离明确的标记形式（emo 标签、停顿标签、纯英文拟声括号/星号），
    正常的中文括号内容一律保留。清理后为空则由调用方决定不发送。"""
    cleaned = text or ""
    cleaned = _NOTIFY_EMO_TAG.sub("", cleaned)
    cleaned = _NOTIFY_PAUSE_TAG.sub("", cleaned)
    cleaned = _NOTIFY_EN_PAREN.sub("", cleaned)
    cleaned = _NOTIFY_EN_ASTERISK.sub("", cleaned)
    cleaned = _NOTIFY_EXTRA_WS.sub(" ", cleaned)
    return cleaned.strip()


@register("astrbot_plugin_elysia_sing", "packy", "爱莉唱歌：真唱歌不是朗读，自动定位副歌并裁剪发送语音", "2.0.0")
class ElysiaSing(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        _sing_core.configure_ffmpeg(
            self.config.get("ffmpeg_path", "ffmpeg"),
            self.config.get("ffprobe_path", "ffprobe"),
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(max(1, _num(self.config, "max_concurrent", 2, int)))
        self._user_active: set[str] = set()
        self._user_last_finish: dict[str, float] = {}
        self._state_lock = asyncio.Lock()
        self._notify_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._cleanup_old_files()

    def _cleanup_old_files(self):
        now = time.time()
        retention = max(0.0, _num(self.config, "retention_hours", 24.0, float)) * 3600
        for path in DATA_DIR.iterdir():
            try:
                age = now - path.stat().st_mtime
                if path.name.startswith("tmp_") and age > 600:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                elif path.name.startswith("sing_") and age > retention:
                    path.unlink()
            except OSError as exc:
                logger.warning("[elysia_sing] 清理文件失败 path=%s err=%s", path, exc)

    async def _send_text(self, event, text):
        await event.send(event.chain_result([Comp.Plain(text)]))

    @staticmethod
    def _failure_reason(exc):
        reason = str(exc)
        if "engine_refused_to_sing" in reason:
            return "这首歌没能唱出来，可能曲库里没有"
        if "音频过短" in reason:
            return "没唱完整，音频太短无法使用"
        if "重合比例过低" in reason:
            return "唱出来的不是这首歌，可能歌名没被认出来"
        return "遇到了技术问题，这次没能唱出来"

    async def _notify_main_agent(self, event, song, outcome):
        if not bool(self.config.get("notify_llm_on_done", True)):
            return
        try:
            from astrbot.core.agent.tool import ToolSet
            from astrbot.core.astr_main_agent import MainAgentBuildConfig, _get_session_conv, build_main_agent
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.provider.entities import ProviderRequest
            from astrbot.core.utils.history_saver import persist_agent_history
            session = MessageSession.from_str(event.unified_msg_origin)
            async with self._notify_locks.setdefault(event.unified_msg_origin, asyncio.Lock()):
                bg_event = CronMessageEvent(
                    context=self.context, session=session,
                    message="唱歌后台任务结果：" + json.dumps(outcome, ensure_ascii=False),
                    sender_id=str(event.get_sender_id() or "astrbot"), sender_name="ElysiaSing",
                    extras={"background_task_result": outcome}, message_type=session.message_type,
                )
                cfg = self.context.get_config(umo=bg_event.unified_msg_origin)
                ps = cfg.get("provider_settings", {}) or {}
                config = MainAgentBuildConfig(
                    tool_call_timeout=ps.get("tool_call_timeout", 120), llm_safety_mode=False,
                    streaming_response=False, provider_settings=ps, add_cron_tools=False,
                )
                # Read the conversation only after completion, while holding the per-session lock.
                conv = await _get_session_conv(event=bg_event, plugin_context=self.context)
                req = ProviderRequest(conversation=conv, func_tool=ToolSet())
                context = json.loads(conv.history or "[]")
                if context:
                    req.contexts = context
                    dump = req._print_friendly_context()
                    req.contexts = []
                    req.system_prompt += "\n以下是截至任务完成时的最新对话：\n---\n" + dump + "\n---\n"
                req.system_prompt += "\n这是延迟完成的后台唱歌结果。本轮禁止调用 elysia_sing 或任何唱歌工具，不得自动重试。"
                req.system_prompt += (
                    "\n回复要简短自然，不要输出任何动作、神态、拟声描写"
                    "（如 (chuckle)、*微笑*、(轻笑) 这类），只输出说出口的话；"
                    "也不要输出 [[emo:...]]、<#数字#> 这类内部标记。"
                )
                if outcome["status"] == "success":
                    req.system_prompt += "这是一个延迟完成的后台任务结果，距离用户点歌可能已经过了一两分钟，期间用户可能聊了别的。请结合最新对话上下文自然地说一句，不要假设用户还在原地等待，也不要重复‘稍等’之类的话。语音已经发出去了，可以自然地提一下唱的是哪段。"
                else:
                    req.system_prompt += f"唱歌失败了，原因是{outcome['reason']}。请用你自己的话告诉用户，如果是曲库里没有这首歌或听错歌名导致的，可以主动提议换一首你会唱的。不要重复道歉，也不要暴露技术细节。"
                req.prompt = "后台唱歌任务结果：" + json.dumps(outcome, ensure_ascii=False) + "。只输出给用户的一句自然短话，不要调用工具。"
                built = await build_main_agent(event=bg_event, plugin_context=self.context, config=config, req=req)
                if not built:
                    logger.warning("[elysia_sing] 主动通知未能构建主 agent song=%s", song)
                    return
                built.provider_request.func_tool.remove_tool("elysia_sing")
                runner = built.agent_runner
                async for _ in runner.step_until_done(max(1, _num(self.config, "notify_max_steps", 8, int))):
                    pass
                resp = runner.get_final_llm_resp()
                if resp and resp.completion_text:
                    send_text = _clean_notify_text(resp.completion_text)
                    if send_text:
                        await self.context.send_message(bg_event.unified_msg_origin, bg_event.chain_result([Comp.Plain(send_text)]))
                    else:
                        logger.warning("[elysia_sing] 主动通知文本清理后为空，跳过发送 song=%s raw=%r", song, resp.completion_text)
                await persist_agent_history(self.context.conversation_manager, event=bg_event, req=req, summary_note="[唱歌后台任务] " + (resp.completion_text if resp and resp.completion_text else json.dumps(outcome, ensure_ascii=False)))
        except Exception as exc:
            logger.warning("[elysia_sing] 主动唤起主 agent 失败 song=%s err_type=%s", song, type(exc).__name__)

    async def _run_job(self, event, user_id, job_id, song, request, hint_lyrics):
        acquired = False
        ran_pipeline = False
        work_dir = DATA_DIR / f"tmp_{job_id}"
        try:
            await asyncio.wait_for(
                self._sem.acquire(),
                timeout=max(0.0, _num(self.config, "queue_timeout", 120.0, float)),
            )
            acquired = True
            work_dir.mkdir(parents=True, exist_ok=True)
            ran_pipeline = True
            final_tmp, details = await _sing_core.execute_pipeline(
                self.config, work_dir, song, request or f"请唱{song}", hint_lyrics
            )
            final_path = DATA_DIR / f"sing_{job_id}.wav"
            final_tmp.replace(final_path)
            logger.info("[elysia_sing] 完成 job=%s user=%s details=%s", job_id, user_id, details)
            await event.send(event.chain_result([Comp.Record(file=str(final_path), url=str(final_path))]))
            trim = details.get("trim", {}) or {}
            method = trim.get("method") or "asr_phrase"
            await self._notify_main_agent(event, song, {
                "status": "success", "song": song, "segment": request,
                "audio_sent": True,
                "duration_seconds": round(float(trim.get("out_dur") or details.get("duration") or 0), 2),
                "location_method": method,
                "location_meaning": (
                    "精准命中了用户指定的片段"
                    if method == "lyrics_match"
                    else "唱的是这首歌的其他段落，不是用户指定的那段，音频已经发出"
                ),
            })
        except asyncio.TimeoutError:
            logger.warning("[elysia_sing] 等待并发槽位超时 job=%s user=%s", job_id, user_id)
            await self._notify_main_agent(event, song, {
                "status": "failed", "song": song, "reason": "排队超过两分钟已取消"
            })
        except asyncio.CancelledError:
            logger.info("[elysia_sing] 插件终止，取消任务 job=%s user=%s", job_id, user_id)
            raise
        except Exception as exc:
            logger.exception("[elysia_sing] 后台任务失败 job=%s user=%s", job_id, user_id)
            await self._notify_main_agent(event, song, {
                "status": "failed", "song": song, "reason": self._failure_reason(exc)
            })
        finally:
            if acquired:
                self._sem.release()
            shutil.rmtree(work_dir, ignore_errors=True)
            async with self._state_lock:
                self._user_active.discard(user_id)
                if ran_pipeline:
                    self._user_last_finish[user_id] = time.monotonic()

    @filter.llm_tool(name="elysia_sing")
    async def elysia_sing(self, event: AstrMessageEvent, song: str, request: str = "",
                          part: str = "", hint_lyrics: str = "") -> str:
        """让爱莉唱一小段歌,结果以语音消息发送。工具会立即返回,语音消息随后自动推送——快则很快,慢则可能需要一段时间,取决于歌曲长度和网络,所以回复用户时简短说一句“稍等一下”就好,不要强调具体等待时间,也不要说“已经发给你了”。每次唱出的长度约 20-25 秒,不是整首歌。同一用户有冷却时间,短时间内重复点歌会被拒绝。有些歌曲可能唱不出来而返回失败原因,这时应自然地告诉用户换一首。

        Args:
            song(string): 用户指定的歌名;用户没指定具体歌曲时,请自己挑一首适合当下气氛的,不要总选同一首。
            request(string): 演唱指令,简短,以歌名为核心,使用“请唱{歌名}”这类模板形式;留空则自动用“请唱{song}”。
            part(string): 想要的片段位置,如“副歌”“高潮”或“开头”;留空则唱开头并裁剪到约 25 秒。
            hint_lyrics(string): 当用户要求副歌、高潮或某几句等特定片段时,应先用联网搜索查这首歌该片段的歌词原文,把几句歌词填入此参数;工具会用它在唱出的音频里做文本匹配以精准定位。搜不到才留空,此时会退化成从开头裁剪约 25 秒。
        """
        logger.info("elysia_sing tool entry: song=%r request=%r part=%r hint_lyrics_len=%d hint_lyrics_preview=%r",
                    song, request, part, len(hint_lyrics or ""), (hint_lyrics or "")[:20])
        if not bool(self.config.get("enabled", True)):
            return "系统提示：唱歌功能当前未启用，无法执行。请告知用户这个功能暂时不可用。"
        song = (song or "").strip()
        if not song:
            return "系统提示：调用失败，song 参数为空。请向用户确认想听哪首歌。"
        user_id = str(event.get_sender_id() or event.unified_msg_origin)
        now = time.monotonic()
        cooldown = max(0.0, _num(self.config, "cooldown", 120.0, float))
        async with self._state_lock:
            if user_id in self._user_active:
                return "系统提示：该用户上一首歌还在处理中，本次请求已拒绝。请让用户等当前这首的语音发出来后再点。"
            remaining = cooldown - (now - self._user_last_finish.get(user_id, -1e12))
            if remaining > 0:
                return (
                    f"系统提示：该用户仍在冷却中，还需约 {int(remaining) + 1} 秒才能再次点歌，"
                    "本次请求已拒绝。请告知用户还需等待多久。"
                )
            self._user_active.add(user_id)
        job_id = uuid.uuid4().hex[:12]
        effective_request = (request or "").strip() or f"请唱{song}"
        if part and part.strip() and part.strip() not in effective_request:
            effective_request = f"{effective_request}，唱{part.strip()}部分"
        try:
            task = asyncio.create_task(
                self._run_job(event, user_id, job_id, song, effective_request, (hint_lyrics or "").strip()),
                name=f"elysia-sing-{job_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            async with self._state_lock:
                self._user_active.discard(user_id)
            raise
        return (
            "系统提示：唱歌任务已提交，正在后台处理，语音消息通常十几秒到一分多钟不等才会自动发出"
            "（取决于歌曲长度和网络），具体时间不确定，可能很快就完成。"
            "请用你自己的话简短告诉用户稍等一下即可，不要强调具体等待时长，也不要说已经发送完成。"
        )

    async def terminate(self):
        try:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()
