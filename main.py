"""liuguang_sing: 流光翻唱插件。

群里 @流光 翻唱（可加风格描述），并引用或附带一个 mp3 文件：
1. 从群消息/引用消息中取音频文件，下载到本地；
2. 调 MiniMax music-cover-free 生成翻唱；
3. 把结果音频 URL 通过 OneBot11 record 消息发回群（NapCat 自动转码 silk）。

仅对 aiocqhttp 平台（OneBot11/NapCat）生效。
"""

import base64
import re
import time

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

_MAX_AUDIO_BYTES = 50 * 1024 * 1024  # MiniMax 限制 50MB
_MIN_PROMPT_LEN = 10
_MAX_PROMPT_LEN = 300
_GENERATE_TIMEOUT = 320  # 翻唱生成较慢，给足超时


class LiuguangSing(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.last_done: float = 0.0
        self.active_jobs: set = set()  # 防止同一用户重复提交（按 group:user）

    # ---------------- 工具 ----------------

    def _is_aiocqhttp(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp"

    @staticmethod
    def _fmt(tmpl: str, **kw) -> str:
        try:
            return tmpl.format(**kw)
        except Exception:
            return tmpl

    def _iter_segments(self, message):
        """遍历 OneBot11 message（list 或 str），产出 dict 段。"""
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    yield seg
        elif isinstance(message, str):
            for m in re.finditer(r"\[CQ:([a-z_]+),([^\]]*)\]", message):
                seg_type = m.group(1)
                data = {}
                for kv in m.group(2).split(","):
                    if "=" in kv:
                        k, _, v = kv.partition("=")
                        data[k.strip()] = v.strip()
                yield {"type": seg_type, "data": data}

    def _find_audio_seg(self, message):
        """从消息里找 file 段（v1 仅支持文件，语音 silk 暂不支持）。"""
        for seg in self._iter_segments(message):
            if seg.get("type") == "file":
                return seg, seg.get("data") or {}
        return None, {}

    async def _resolve_audio_bytes(self, event, group_id: str, raw: dict):
        """解析音频文件为 bytes。返回 (bytes, 扩展名) 或 None。"""
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        if not isinstance(event, AiocqhttpMessageEvent) or not event.bot:
            return None
        client = event.bot

        # 1) 本消息里找文件
        seg, data = self._find_audio_seg(raw.get("message"))
        reply_id = None
        if seg is None:
            # 2) 找 reply 引用，拉取被引用消息
            for s in self._iter_segments(raw.get("message")):
                if s.get("type") == "reply":
                    reply_id = (s.get("data") or {}).get("id")
                    break
            if reply_id:
                try:
                    resp = await client.call_action("get_msg", message_id=int(reply_id))
                except Exception as e:
                    logger.warning(f"[liuguang_sing] get_msg 失败: {e}")
                    resp = None
                if isinstance(resp, dict):
                    inner = resp.get("data") or resp
                    seg, data = self._find_audio_seg(inner.get("message"))

        if seg is None:
            return None

        seg_type = seg.get("type")
        url = (data.get("url") or "").strip()
        file_id = (data.get("file_id") or data.get("file") or "").strip()
        name = str(data.get("file") or data.get("name") or "")

        # 群文件：无 url 时用 get_group_file_url 换直链
        if not url and file_id and seg_type == "file":
            try:
                resp = await client.call_action(
                    "get_group_file_url", file_id=file_id, group=group_id
                )
                if isinstance(resp, dict):
                    d = resp.get("data") or resp
                    url = str(d.get("url") or "").strip()
            except Exception as e:
                logger.warning(f"[liuguang_sing] get_group_file_url 失败: {e}")

        if not url:
            return None

        # 下载
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as hc:
                resp = await hc.get(url)
                if resp.status_code != 200:
                    logger.warning(f"[liuguang_sing] 下载音频 HTTP {resp.status_code}")
                    return None
                content = resp.content
        except Exception as e:
            logger.error(f"[liuguang_sing] 下载音频失败: {e}")
            return None

        if not content or len(content) > _MAX_AUDIO_BYTES:
            return None

        ext = "mp3"
        m = re.search(r"\.([A-Za-z0-9]{2,4})$", (name or url).split("?")[0])
        if m:
            ext = m.group(1).lower()
        if ext not in ("mp3", "wav", "flac", "m4a", "aac", "ogg", "opus"):
            ext = "mp3"  # 未知扩展名按 mp3 处理
        return content, ext

    async def _send_group_text(self, event, group_id: str, text: str):
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            if isinstance(event, AiocqhttpMessageEvent) and event.bot:
                await event.bot.call_action(
                    "send_group_msg", group_id=group_id, message=text
                )
                return
        except Exception as e:
            logger.error(f"[liuguang_sing] 发送文本失败: {e}")
        try:
            from astrbot.api.message_components import Comp
            from astrbot.api.event import MessageChain

            await self.context.send_message(
                event.unified_msg_origin, MessageChain([Comp.Plain(text)])
            )
        except Exception as e:
            logger.error(f"[liuguang_sing] 回退发送失败: {e}")

    # ---------------- 事件处理 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_sing_command(self, event: AstrMessageEvent):
        if not self._is_aiocqhttp(event):
            return
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "raw_message"):
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "message" or raw.get("message_type") != "group":
            return

        # 必须 @ 机器人
        self_id = str(raw.get("self_id", ""))
        at_bot = False
        for s in self._iter_segments(raw.get("message")):
            if s.get("type") == "at" and str((s.get("data") or {}).get("qq", "")) == self_id:
                at_bot = True
                break
        if not at_bot:
            return

        text = (event.message_str or "").strip()
        if "翻唱" not in text and "cover" not in text.lower():
            return

        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))

        # 冷却与去重
        now = time.time()
        if now - self.last_done < int(self.config.get("cooldown_seconds") or 0):
            remain = int(int(self.config.get("cooldown_seconds") or 0) - (now - self.last_done))
            await self._send_group_text(
                event, group_id, f"🎤 休息一下，{remain} 秒后可以再翻唱~"
            )
            return
        job_key = f"{group_id}:{user_id}"
        if job_key in self.active_jobs:
            await self._send_group_text(event, group_id, "正在翻唱上一首，请稍候~")
            return

        # 风格描述：去掉"翻唱"前后的词后剩余文本
        prompt = re.sub(r"翻唱|cover", "", text, flags=re.I).strip()
        prompt = prompt or self.config.get("default_prompt") or "原曲风格翻唱，深情演绎"
        if len(prompt) < _MIN_PROMPT_LEN:
            prompt = f"{prompt}，翻唱演绎"  # 补足最低长度
        if len(prompt) > _MAX_PROMPT_LEN:
            prompt = prompt[:_MAX_PROMPT_LEN]

        # 取音频
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            if not isinstance(event, AiocqhttpMessageEvent):
                await self._send_group_text(event, group_id, "当前平台不支持翻唱功能~")
                return
        except ImportError:
            await self._send_group_text(event, group_id, "当前平台不支持翻唱功能~")
            return

        got = await self._resolve_audio_bytes(event, group_id, raw)
        if got is None:
            await self._send_group_text(
                event,
                group_id,
                "请先发一个 mp3 文件，然后 @流光 翻唱（可引用该文件消息，或直接附带文件）~",
            )
            return
        audio_bytes, ext = got
        if len(audio_bytes) < 60000:
            await self._send_group_text(event, group_id, "音频太短了，至少 6 秒~")
            return

        api_key = (self.config.get("api_key") or "").strip()
        if not api_key:
            await self._send_group_text(event, group_id, "未配置 MiniMax API Key~")
            return

        self.active_jobs.add(job_key)
        await self._send_group_text(
            event, group_id, self.config.get("waiting_msg") or "🎵 收到！正在翻唱中~"
        )

        try:
            url = await self._generate_cover(
                api_key,
                self.config.get("api_base") or "https://api.minimax.chat",
                self.config.get("model") or "music-cover-free",
                prompt,
                audio_bytes,
            )
        except Exception as e:
            logger.error(f"[liuguang_sing] 翻唱失败: {e}")
            msg = self._fmt(self.config.get("fail_msg") or "翻唱失败：{err}", err=str(e))
            await self._send_group_text(event, group_id, msg)
            return
        finally:
            self.active_jobs.discard(job_key)

        self.last_done = time.time()

        # 发送语音
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            if isinstance(event, AiocqhttpMessageEvent) and event.bot:
                await event.bot.call_action(
                    "send_group_msg",
                    group_id=group_id,
                    message=[{"type": "record", "data": {"file": url}}],
                )
        except Exception as e:
            logger.error(f"[liuguang_sing] 发送语音失败: {e}")
            await self._send_group_text(event, group_id, f"语音发送失败：{e}，音频链接：{url}")
            return

        success = self.config.get("success_msg")
        if success:
            await self._send_group_text(event, group_id, success)

    async def _generate_cover(self, api_key, api_base, model, prompt, audio_bytes) -> str:
        """调用 MiniMax music_generation 翻唱，返回音频 URL。"""
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        payload = {
            "model": model,
            "prompt": prompt,
            "audio_base64": b64,
            "audio_setting": {
                "sample_rate": 44100,
                "bitrate": 128000,
                "format": "mp3",
            },
            "output_format": "url",
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        endpoint = f"{api_base.rstrip('/')}/v1/music_generation"
        async with httpx.AsyncClient(timeout=_GENERATE_TIMEOUT) as hc:
            resp = await hc.post(endpoint, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"MiniMax HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"响应解析失败: {resp.text[:200]}")

        base_resp = body.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(
                f"MiniMax 错误 {base_resp.get('status_code')}: {base_resp.get('status_msg')}"
            )

        data = body.get("data") or {}
        audio = data.get("audio")
        if isinstance(audio, str) and audio.startswith("http"):
            return audio
        if isinstance(audio, dict):
            url = audio.get("url")
            if url:
                return url
        raise RuntimeError(f"未拿到音频 URL: {str(body)[:200]}")

    async def terminate(self):
        self.active_jobs.clear()