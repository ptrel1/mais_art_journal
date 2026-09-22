"""麦麦绘卷 SDK 2.x 插件入口（Pipeline 架构）

组件：
- @Tool draw_picture: 智能文/图生图，由 LLM/规划器按需调用；委托给 action pipeline
- @Command dr: 单一入口，通过 CommandDispatcher 分发到 @subcommand 注册的 handlers
- @API generate_image: 对外暴露的生图接口，供其他插件 ctx.api.call 调用
- @API list_image_models: 对外暴露的模型清单接口

生命周期：
- on_load: 初始化 dispatcher + 加载 action pipeline + 启动自动自拍
- on_unload: 停止自动自拍
- on_config_update: 版本变更时备份配置 + 同步自动自拍开关
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Optional

from maibot_sdk import API, Command, MaiBotPlugin, Tool

from .core.config.models import MaisArtConfig

if TYPE_CHECKING:
    from .core.commands.dispatcher import CommandDispatcher
    from .core.pipeline import Pipeline
    from .core.selfie.auto_selfie_task import AutoSelfieTask

logger = logging.getLogger("plugin.mais_art_journal")


class MaisArtPlugin(MaiBotPlugin):
    """麦麦绘卷（Claude MAInet）智能多模型图片生成插件"""

    config_model = MaisArtConfig
    config_reload_subscriptions = ()  # 不订阅 bot/model 全局广播

    # 智能生图 Tool 描述（2026-08-23 精简：原 1058 字符过长，导致 MaiBot deferred-tools
    # system-reminder 拼成一行后格式异常、挤占上下文。见 GitHub issue#35。仅精简文本，
    # 功能逻辑 unchanged。保留最核心两条调用规则，具体风格/参数由 Tool 参数与 config 管理。）
    _DRAW_TOOL_DESCRIPTION = (
        "智能图片生成：根据描述生成图片（文生图）或基于现有图片进行修改（图生图）。"
        "自动检测是否有输入图片来决定文生图或图生图模式。"
        "仅在用户明确要求画图、生成图片、自拍或修改图片时使用。"
        "不要响应发给其他机器人的画图命令（如 /nai、/sd、/mj）。"
    )

    _DRAW_TOOL_PARAMETERS = {
        "description": {
            "type": "string",
            "description": "图片描述文本（中文或英文均可），例如用户说'画一只小猫'则填写'一只小猫'。必填。",
            "required": True,
        },
        "msg_id": {
            "type": "string",
            "description": (
                "触发本次工具调用的那条用户消息 ID——也就是聊天记录里最近一条要求你画图的 user 消息的 msg_id（"
                "你在历史消息每条 <message msg_id=\"...\"> 前缀里能看到）。"
                "用于支持图生图：若用户在那条消息里直接附图或引用了一张图片，插件会自动取图。"
                "**必填**，缺失时无法进行图生图，仅能文生图。"
            ),
            "required": True,
        },
        "model_id": {
            "type": "string",
            "description": "可选：要使用的模型 ID（如 model1、model2 等）。不填则由插件按场景自动选择（文生图/图生图/自拍各有独立默认模型）。",
        },
        "strength": {
            "type": "number",
            "description": "可选：图生图强度，0.1-1.0，值越高变化越大。仅图生图时使用，默认 0.7。",
        },
        "size": {
            "type": "string",
            "description": "可选：图片尺寸，如 512x512、1024x1024 等。不指定则由 LLM 智能选或用模型默认尺寸。",
        },
        "selfie_mode": {
            "type": "boolean",
            "description": "可选：是否启用自拍模式（默认 false）。启用后会自动添加自拍场景和手部动作。",
        },
        "selfie_style": {
            "type": "string",
            "description": "自拍风格：standard（前置自拍）/ mirror（对镜自拍）/ photo（第三人称照片）。仅 selfie_mode=true 时生效。",
            "enum": ["standard", "mirror", "photo"],
        },
        "free_hand_action": {
            "type": "string",
            "description": "自由手部动作描述（英文）。指定后将使用此动作而非随机生成。仅 selfie_mode=true 时生效。",
        },
    }

    def __init__(self):
        super().__init__()
        self._plugin_dir: str = os.path.dirname(os.path.abspath(__file__))
        self._dispatcher: Optional["CommandDispatcher"] = None
        self._action_pipeline: Optional["Pipeline"] = None
        self._auto_selfie_task: Optional["AutoSelfieTask"] = None

    # ── 公共属性 ──────────────────────────────────

    @property
    def plugin_dir(self) -> str:
        """插件目录绝对路径"""
        return self._plugin_dir

    def get_config(self, key: str, default: Any = None) -> Any:
        """嵌套 dot-path 配置读取（保留给 runtime_state / model_registry 等历史接口）"""
        config = self.get_plugin_config_data()
        current: Any = config
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current if current is not None else default

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        from .core.commands.dispatcher import CommandDispatcher
        from .core.pipeline import build_action_pipeline

        self._dispatcher = CommandDispatcher(self)
        self._action_pipeline = build_action_pipeline()

        if self.config.selfie.auto_enabled:
            from .core.selfie.auto_selfie_task import AutoSelfieTask
            task = AutoSelfieTask(self)
            await task.start()  # 成功后才赋值
            self._auto_selfie_task = task

        logger.info(f"麦麦绘卷已加载 (v{self.config.plugin.config_version}, Pipeline 架构)")

    async def on_unload(self) -> None:
        if self._auto_selfie_task is not None:
            try:
                await self._auto_selfie_task.stop()
            except Exception as e:
                logger.warning(f"自动自拍任务停止失败: {e}")
        logger.info("麦麦绘卷已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return

        # 触发版本备份
        from .core.config.backup import backup_config_if_version_changed
        expected_version = self.config.plugin.config_version
        current_version = ""
        try:
            if isinstance(config_data, dict):
                plugin_section = config_data.get("plugin")
                if isinstance(plugin_section, dict):
                    current_version = str(plugin_section.get("config_version", ""))
        except Exception:
            current_version = ""
        backup_config_if_version_changed(
            self._plugin_dir, "config.toml", expected_version, current_version,
        )

        # 自动自拍随配置同步
        try:
            if self.config.selfie.auto_enabled:
                if self._auto_selfie_task is None:
                    from .core.selfie.auto_selfie_task import AutoSelfieTask
                    self._auto_selfie_task = AutoSelfieTask(self)
                    await self._auto_selfie_task.start()
            else:
                if self._auto_selfie_task is not None:
                    await self._auto_selfie_task.stop()
                    self._auto_selfie_task = None
        except Exception as e:
            logger.warning(f"配置更新时调整自动自拍任务失败: {e}")

    # ── Tool: 智能生图 ──────────────────────────

    @Tool(
        "draw_picture",
        description=_DRAW_TOOL_DESCRIPTION,
        parameters=_DRAW_TOOL_PARAMETERS,
    )
    async def handle_draw_picture(self, **kwargs: Any):
        """智能生图入口（@Tool），LLM/规划器调用时由 host 把 function_args 平铺进 kwargs，
        同时附带 stream_id / chat_id / group_id / user_id / platform 上下文字段。
        所有逻辑委托给 action pipeline。
        """
        if self._action_pipeline is None:
            logger.error("Action pipeline 尚未初始化")
            return False, "插件未就绪"

        from .core.pipeline import build_request_from_tool_kwargs, make_pipeline_context

        req = await build_request_from_tool_kwargs(self, kwargs)
        if req is None:
            return False, "已跳过"

        ctx = make_pipeline_context(self, req)
        out = await self._action_pipeline.run(req, ctx)
        return out.success, out.user_message or out.error or ("生成成功" if out.success else "生成失败")

    # ── API: 对外暴露给其他插件调用 ───────────────────

    @API(
        "generate_image",
        description=(
            "生成图片（文生图 / 图生图，自动识别）并返回 base64，不发送到聊天流。"
            "其他插件可通过 ctx.api.call('1021143806.mais_art_journal.generate_image', "
            "prompt=..., model_id=..., input_image_base64=..., ...) 复用本插件配置的全部模型。"
        ),
        version="1",
        public=True,
    )
    async def api_generate_image(
        self,
        prompt: str = "",
        model_id: str = "",
        size: str = "",
        strength: Optional[float] = None,
        input_image_base64: Optional[str] = None,
        negative_prompt: str = "",
        selfie_mode: bool = False,
        selfie_style: str = "standard",
        free_hand_action: str = "",
        use_cache: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """对外 API：生成图片，返回 base64

        Args:
            prompt: 图片描述（必填，中英文均可；启用 prompt_optimizer 时会自动优化为英文 SD 提示词）
            model_id: 模型 ID（如 model1、model2），留空时按文生图/图生图场景自动选择默认模型
            size: 图片尺寸，如 "1024x1024"；留空由 LLM 自选或用模型默认
            strength: 图生图强度 0.1-1.0；仅 input_image_base64 非空时生效，默认 0.7
            input_image_base64: 图生图输入图（纯 base64 字符串，不含 data:image/... 前缀）
            negative_prompt: 额外负面提示词（合并到模型自身的 negative_prompt_add）
            selfie_mode: 是否启用自拍模式（默认 false）
            selfie_style: 自拍风格 standard/mirror/photo（仅 selfie_mode=true 生效）
            free_hand_action: 自由手部动作（英文，仅 selfie_mode=true 生效）
            use_cache: 是否参与缓存读写（默认 false，每次都重新生成）

        Returns:
            Dict[str, Any]: {
                "success": bool,                # 是否成功
                "image_base64": str,            # 成功时为生成图的 base64（不含前缀）
                "model_id": str,                # 实际使用的模型 ID
                "size": str,                    # 实际使用的尺寸
                "is_img2img": bool,             # 是否走的图生图分支
                "error": str,                   # 失败时的错误描述
            }

        Examples:
            ::

                result = await self.ctx.api.call(
                    "1021143806.mais_art_journal.generate_image",
                    prompt="一只在月光下打哈欠的银发狐妖",
                    model_id="model1",
                    size="1024x1024",
                )
                if result["success"]:
                    image_bytes = base64.b64decode(result["image_base64"])
        """
        del kwargs  # 预留扩展，当前未使用

        if not prompt or not str(prompt).strip():
            return {
                "success": False,
                "error": "prompt 不能为空",
                "image_base64": "",
                "model_id": "",
                "size": "",
                "is_img2img": False,
            }

        # 参数标准化
        strength_f: Optional[float] = None
        if strength is not None:
            try:
                strength_f = float(strength)
                if not (0.1 <= strength_f <= 1.0):
                    strength_f = 0.7
            except (ValueError, TypeError):
                strength_f = 0.7

        style = str(selfie_style or "standard").strip().lower()
        if style not in ("standard", "mirror", "photo"):
            style = "standard"

        # 构造 pipeline 请求（source=standalone：跳过 chat-stream 级的模型禁用检查）
        from .core.pipeline import (
            GenerationRequest,
            build_action_pipeline,
            make_pipeline_context,
        )

        req = GenerationRequest(
            description=str(prompt).strip(),
            model_id=str(model_id or "").strip(),
            size=str(size or "").strip(),
            strength=strength_f,
            input_image_base64=input_image_base64 or None,
            extra_negative_prompt=str(negative_prompt or "").strip() or None,
            is_selfie=bool(selfie_mode),
            selfie_style=style,  # type: ignore[arg-type]
            free_hand_action=str(free_hand_action or "").strip(),
            send_image=False,
            update_cache=bool(use_cache),
            schedule_recall=False,
            debug_info=False,
            silent_img2img_fallback=True,
            stream_id="",
            chat_id="",
            log_prefix="[API]",
            source="standalone",
        )

        pipeline = self._action_pipeline if self._action_pipeline is not None else build_action_pipeline()
        pipeline_ctx = make_pipeline_context(self, req)
        out = await pipeline.run(req, pipeline_ctx)

        return {
            "success": bool(out.success),
            "image_base64": out.resolved_image_data or "",
            "model_id": req.model_id,
            "size": req.final_size or req.size,
            "is_img2img": bool(req.is_img2img),
            "error": out.error or "",
        }

    @API(
        "list_image_models",
        description="列出当前插件已配置的图片生成模型（id / 名称 / API 格式 / 是否支持图生图）",
        version="1",
        public=True,
    )
    async def api_list_image_models(self, **kwargs: Any) -> Dict[str, Any]:
        """对外 API：列出可用模型

        Returns:
            Dict[str, Any]: {
                "success": bool,
                "default_model": str,           # basic.default_txt2img_model（兼容旧调用方）
                "default_txt2img_model": str,   # basic.default_txt2img_model
                "default_img2img_model": str,   # basic.default_img2img_model（留空时与文生图相同）
                "models": list[dict],           # [{"id", "name", "format", "model", "support_img2img"}]
            }
        """
        del kwargs
        try:
            from .core.config import (
                list_models,
                resolve_img2img_model,
                resolve_txt2img_model,
            )
            items = list_models(self)
            models = [
                {
                    "id": mid,
                    "name": cfg.get("name", ""),
                    "format": cfg.get("format", ""),
                    "model": cfg.get("model", ""),
                    "support_img2img": bool(cfg.get("support_img2img", True)),
                    "default_size": cfg.get("default_size", ""),
                }
                for mid, cfg in items.items()
            ]
            return {
                "success": True,
                "default_model": resolve_txt2img_model(self),
                "default_txt2img_model": resolve_txt2img_model(self),
                "default_img2img_model": resolve_img2img_model(self),
                "models": models,
            }
        except Exception as exc:
            logger.error(f"api_list_image_models 失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc), "models": []}

    # ── Command: /dr 全套（集中分发） ───────────────

    @Command(
        "dr",
        description="麦麦绘卷命令套件（生图 / 模型管理 / 风格管理 / 自拍开关）。命令前缀可在「基础配置.命令前缀」修改后重启 MaiBot 生效",
        pattern=r"^\s*(?:\[(?=[^\]]*(?:图片|image|表情|emoji))(?:[^\[\]]|\[[^\]]*\])*\]\s*)*(?:.*，说：\s*)?/dr(?:\s+(?P<rest>.+))?$",
    )
    async def handle_dr(self, **kwargs: Any):
        """命令入口：实际 pattern 由 get_components() 按 config.basic.command_prefix 动态注入

        装饰器里的 pattern 仅是默认占位（/dr）。get_components() 返回 host 之前会按
        当前 config.basic.command_prefix 重写 metadata.command_pattern 为精确前缀，
        避免抢占其他插件的命令（如 /mute on）。修改前缀后**重启 MaiBot** 即生效。
        """
        if self._dispatcher is None:
            logger.error("CommandDispatcher 尚未初始化")
            return False, "插件未就绪", True

        matched = kwargs.get("matched_groups") or {}
        message = kwargs.get("message")

        # host 平铺进来的上下文字段（component_query._build_command_executor 注入）
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        group_id = str(kwargs.get("group_id") or "")

        # 昵称/群名仅在 message dict 里携带，按需取
        user_nickname, group_name = _extract_display_names(message)

        # 引用拼接误触发检测：取用户自己实际输入的文本，跳过 reply / image 等段
        configured_prefix = (self.config.basic.command_prefix or "/dr").strip()
        if not configured_prefix.startswith("/"):
            configured_prefix = "/" + configured_prefix
        if _looks_like_quoted_command(message, configured_prefix):
            logger.info("检测到引用了别人的命令消息且用户自身未发命令，透传")
            return False, "quoted command from others", False

        rest = (matched.get("rest") or "").strip()
        return await self._dispatcher.dispatch(
            stream_id, message, rest,
            user_id=user_id,
            group_id=group_id,
            user_nickname=user_nickname,
            group_name=group_name,
        )

    # ── get_components 覆写：动态注入用户配置的精确命令前缀 ──

    def get_components(self) -> list[dict[str, Any]]:
        """注入 basic.command_prefix 到 dr command 的 pattern，避免宽匹配抢占其他插件"""
        components = super().get_components()

        # 使用 _plugin_config_data 而不是 self.config，避免配置未注入时的 RuntimeError
        try:
            basic_section = self._plugin_config_data.get("basic", {})
            if isinstance(basic_section, dict):
                prefix = basic_section.get("command_prefix", "/dr")
            else:
                prefix = "/dr"
        except Exception:
            prefix = "/dr"

        prefix = str(prefix or "/dr").strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        escaped = re.escape(prefix)
        # ^ 锚反"别的 bot 帮助文本"误触；放行前导空白和"含图片/表情"的方括号占位段
        # （覆盖附图/附 emoji/引用 这几种 processed_plain_text 形态）。
        bracket_media = (
            r"\[(?=[^\]]*(?:图片|image|表情|emoji))"
            r"(?:[^\[\]]|\[[^\]]*\])*\]"
        )
        new_pattern = (
            rf"^\s*(?:{bracket_media}\s*)*"
            rf"(?:.*，说：\s*)?{escaped}(?:\s+(?P<rest>.+))?$"
        )

        for comp in components:
            if (
                comp.get("name") == "dr"
                and str(comp.get("type", "")).upper() == "COMMAND"
            ):
                metadata = comp.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    comp["metadata"] = metadata
                metadata["command_pattern"] = new_pattern
                logger.info(f"麦麦绘卷命令前缀生效: {prefix!r} (pattern={new_pattern!r})")
                break

        return components


def create_plugin() -> MaisArtPlugin:
    """SDK Runner 创建插件实例的入口"""
    return MaisArtPlugin()


def _extract_user_text_segments(message: Any) -> tuple[str, bool]:
    """从消息对象提取"用户自身实际输入的纯文本"

    跳过 ReplyComponent / ImageComponent / EmojiComponent / AtComponent 等非用户文本段，
    仅拼接 TextComponent。用于区分"用户自己发的命令"和"引用别人的命令消息后拼接出来的文本"。

    支持三种消息形态：
    1. SDK 2.x 的 RPC 序列化 dict：``message["raw_message"]`` 是 list[dict]，
       每段 ``{"type": "text"|"image"|"reply"|...}``。
    2. SessionMessage（同进程内）：``message.raw_message.components`` 列表。
    3. 老 message_segment 形态：``message.message_segment`` 是 Seg 树。

    Returns:
        (user_text, has_reply): 用户净输入文本 + 该消息是否含引用段
    """
    if message is None:
        return "", False

    parts: list[str] = []
    has_reply = False

    # 路径 1：dict 形态（SDK 2.x command 入口）
    if isinstance(message, dict):
        raw = message.get("raw_message")
        if isinstance(raw, list):
            for seg in raw:
                if not isinstance(seg, dict):
                    continue
                stype = str(seg.get("type") or "").lower()
                if stype == "reply":
                    has_reply = True
                    continue
                if stype == "text":
                    text = seg.get("data")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts).strip(), has_reply
        # raw_message 不是 list（罕见）→ 兜底走 reply_to 标记
        if message.get("reply_to"):
            has_reply = True
        return "", has_reply

    # 路径 2：SessionMessage / MaiMessage —— message.raw_message.components
    raw_msg = getattr(message, "raw_message", None)
    components = getattr(raw_msg, "components", None) if raw_msg is not None else None
    if isinstance(components, (list, tuple)) and components:
        for comp in components:
            cls_name = type(comp).__name__
            if cls_name == "ReplyComponent":
                has_reply = True
                continue
            if cls_name == "TextComponent":
                text = getattr(comp, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                continue
            # 其他组件（Image/Emoji/At/Voice/Forward）忽略
        return "".join(parts).strip(), has_reply

    # 路径 3：老 Seg 树 —— message.message_segment
    seg = getattr(message, "message_segment", None)
    if seg is not None:
        def _walk(s: Any) -> None:
            nonlocal has_reply
            if s is None:
                return
            stype = getattr(s, "type", None)
            sdata = getattr(s, "data", None)
            if stype == "reply":
                has_reply = True
                return
            if stype == "text" and isinstance(sdata, str):
                parts.append(sdata)
                return
            if stype == "seglist" and isinstance(sdata, (list, tuple)):
                for child in sdata:
                    _walk(child)

        _walk(seg)
        return "".join(parts).strip(), has_reply

    # 路径 4：兜底 —— message.reply_to 非空表示这是引用消息
    reply_to = getattr(message, "reply_to", None)
    if reply_to:
        has_reply = True
    return "", has_reply


def _extract_display_names(message: Any) -> tuple[str, str]:
    """从消息对象提取 (user_nickname, group_name)，仅用于日志前缀。

    优先 SDK 2.x dict 形态，其次老对象形态；取不到返回空串。
    """
    if message is None:
        return "", ""

    if isinstance(message, dict):
        msg_info = message.get("message_info") or {}
        if not isinstance(msg_info, dict):
            return "", ""
        user_info = msg_info.get("user_info") or {}
        group_info = msg_info.get("group_info") or {}
        nick = ""
        gname = ""
        if isinstance(user_info, dict):
            nick = str(user_info.get("user_nickname") or user_info.get("user_cardname") or "")
        if isinstance(group_info, dict):
            gname = str(group_info.get("group_name") or "")
        return nick, gname

    msg_info = getattr(message, "message_info", None)
    if msg_info is None:
        return "", ""
    user_info = getattr(msg_info, "user_info", None)
    group_info = getattr(msg_info, "group_info", None)
    nick = ""
    gname = ""
    if user_info is not None:
        nick = str(getattr(user_info, "user_nickname", "") or getattr(user_info, "user_cardname", "") or "")
    if group_info is not None:
        gname = str(getattr(group_info, "group_name", "") or "")
    return nick, gname


def _looks_like_quoted_command(message: Any, configured_prefix: str) -> bool:
    """判断 processed_plain_text 的命令命中是不是"引用别人命令消息"误触发

    判定：消息含 reply 段 + 用户自己的纯文本不以 /<prefix> 开头 → 误触发。
    若没有 reply 段（用户自己发的命令），返回 False。
    若有 reply 段但用户自己也输入了 /<prefix> ...（如对图发 /dv cartoon），返回 False。
    """
    user_text, has_reply = _extract_user_text_segments(message)
    if not has_reply:
        return False
    # 用户自己的输入为空 或 不以前缀开头 → 误触发
    prefix_with_slash = configured_prefix if configured_prefix.startswith("/") else "/" + configured_prefix
    if not user_text:
        return True
    lower_text = user_text.lstrip().lower()
    lower_prefix = prefix_with_slash.lower()
    if not (lower_text == lower_prefix or lower_text.startswith(lower_prefix + " ")):
        return True
    return False
