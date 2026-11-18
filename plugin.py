"""麦麦绘卷 SDK 2.x 插件入口（Pipeline 架构）

组件：
- @Tool draw_picture: 智能文/图生图，由 LLM/规划器按需调用；委托给 action pipeline
- @Command dr: 单一入口，通过 CommandDispatcher 分发到 @subcommand 注册的 handlers
- @API generate_image: 对外暴露的生图接口，供其他插件 ctx.api.call 调用
- @API list_image_models: 对外暴露的模型清单接口

生命周期：
- on_load: 初始化 dispatcher + 加载 action pipeline + 启动自动自拍
- on_unload: 停止自动自拍
- create_plugin: 在宿主重建配置前迁移并备份旧配置
- on_config_update: 同步自动自拍和群图任务
"""
from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from maibot_sdk import API, Command, MaiBotPlugin, Tool

import asyncio
import logging
import os
import re

from .core.config.models import MaisArtConfig
from .core.draw_limit import DrawLimiter, resolve_draw_message
from .core.quota import QuotaStore
from .core.state import ProactiveStateStore, RuntimeStateManager
from .core.webui.trial import TrialManager

if TYPE_CHECKING:
    from .core.commands.dispatcher import CommandDispatcher
    from .core.pipeline import Pipeline
    from .core.selfie.auto_selfie_task import AutoSelfieTask
    from .core.selfie.group_image_task import GroupImageTask

logger = logging.getLogger("plugin.mais_art_journal")


def _tracked_operation(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """保持 SDK 组件签名及元数据，记录卸载前必须结束的生图和管理调用。"""
    @wraps(function)
    async def wrapped(plugin: "MaisArtPlugin", *args: Any, **kwargs: Any) -> Any:
        if plugin._closing:
            raise RuntimeError("绘卷正在卸载，已停止接受新的生图和管理请求")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("绘卷调用缺少当前异步任务")
        plugin._active_operations[task] = plugin._active_operations.get(task, 0) + 1
        try:
            return await function(plugin, *args, **kwargs)
        finally:
            count = plugin._active_operations[task] - 1
            if count:
                plugin._active_operations[task] = count
            else:
                del plugin._active_operations[task]
    return wrapped


def _generation_failure(error: str) -> Dict[str, Any]:
    """参数校验失败时也保留完整的公开 API 返回结构。"""
    return {
        "success": False, "image_base64": "", "model_id": "", "size": "",
        "is_img2img": False, "error": error, "selfie_style": "", "outfit": "",
    }


class MaisArtPlugin(MaiBotPlugin):
    """麦麦绘卷（Claude MAInet）智能多模型图片生成插件"""

    config_model = MaisArtConfig
    config_reload_subscriptions = ()  # 不订阅 bot/model 全局广播

    @classmethod
    def build_config_schema(cls, **kwargs: Any) -> Dict[str, Any]:
        """按使用流程展示配置，保存时仍使用原有字段和配置路径。"""
        from .core.config.ui_schema import build_config_ui
        return build_config_ui(super().build_config_schema(**kwargs))

    def get_webui_config_schema(self, **kwargs: Any) -> Dict[str, Any]:
        """在模型编号输入框旁列出已保存的编号与名称，使用宿主现有提示文字。"""
        schema = super().get_webui_config_schema(**kwargs)
        config = self.get_plugin_config_data()
        enabled = config.get("plugin", {}).get("enabled", False)
        schema["sections"]["models"]["description"] = (
            "此处是完整表单，始终显示全部字段。按格式切换参数请打开侧栏「绘卷控制台」。"
            if enabled else
            "当前插件已停用，绘卷控制台尚未加载。启用插件并保存后，可打开侧栏「绘卷控制台」按格式切换参数。此处始终显示完整表单。"
        )
        models = config.get("models", {}).get("items", []) if config else schema["sections"]["models"]["fields"]["items"]["default"]
        model_hint = "；".join(f"{model['id']}：{model['name']}" for model in models)
        selector_paths = {
            "basic.default_txt2img_model", "basic.default_img2img_model", "basic.pic_command_model",
            "selfie.llm_selfie_model", "selfie.selfie_model", "group_image.model_id",
        }
        for section in schema["sections"].values():
            for name, field in section["fields"].items():
                if f"{section['name']}.{name}" in selector_paths and model_hint:
                    field["hint"] += f" 已保存的服务编号：{model_hint}。"
        return schema

    # 智能生图 Tool 描述（2026-08-23 精简：原 1058 字符过长，导致 MaiBot deferred-tools
    # system-reminder 拼成一行后格式异常、挤占上下文。见 GitHub issue#35。仅精简文本，
    # 只保留核心触发与重试约束，具体风格/参数由 Tool 参数与 config 管理。）
    _DRAW_TOOL_DESCRIPTION = (
        "智能图片生成：根据描述生成图片（文生图）或基于现有图片进行修改（图生图）。"
        "自动检测是否有输入图片来决定文生图或图生图模式。"
        "仅在用户明确要求画图、生成图片、自拍或修改图片时使用。"
        "不要响应发给其他机器人的画图命令（如 /nai、/sd、/mj）。"
        "遵守本次用户请求的生图次数上限；不要因自我评估不满意而反复重画。达到上限后等待用户新的要求。"
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
                "必填，必须使用当前聊天最新的用户消息 ID；缺失、过期或不属于当前聊天时拒绝生图。"
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
            "description": "自拍风格：auto（按活动）/ standard（前置自拍）/ mirror（对镜自拍）/ photo（第三人称照片）。仅 selfie_mode=true 时生效。",
            "enum": ["auto", "standard", "mirror", "photo"],
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
        self._group_image_task: Optional["GroupImageTask"] = None
        self.runtime_state = RuntimeStateManager()
        self.proactive_state = ProactiveStateStore()
        self.quota_store = QuotaStore()
        self._draw_limiter = DrawLimiter()
        self._active_operations: Dict[asyncio.Task, int] = {}
        self._recall_tasks: Set[asyncio.Task] = set()
        self._closing = False
        self._cleanup_task: Optional[asyncio.Task] = None
        self._trial_manager = TrialManager(self)

    # ── 公共属性 ──────────────────────────────────

    @property
    def plugin_dir(self) -> str:
        """插件目录绝对路径"""
        return self._plugin_dir

    def get_config(self, key: str, default: Any = None) -> Any:
        """读取当前插件配置中的嵌套字段。"""
        config = self.get_plugin_config_data()
        current: Any = config
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current if current is not None else default

    def normalize_plugin_config(
        self, config_data: Optional[Mapping[str, Any]],
    ) -> Tuple[Dict[str, Any], bool]:
        """SDK 配置入口统一迁移旧字段，业务模块只读取当前结构。"""
        from .core.config.migration import normalize_config_for_upgrade
        return normalize_config_for_upgrade(config_data)

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        from .core.commands.dispatcher import CommandDispatcher
        from .core.pipeline import build_action_pipeline
        from .core.state.migration import migrate_plugin_data

        self._closing = False
        self._cleanup_task = None
        self._trial_manager = TrialManager(self)
        try:
            # 会话覆盖与额度均由插件实例持有；入口和后台任务必须在加载完成后启动。
            data_dir = self.ctx.paths.data_dir
            await asyncio.to_thread(migrate_plugin_data, Path(self.plugin_dir) / "data", data_dir)
            await self.runtime_state.load(data_dir / "session_settings.json")
            await self.quota_store.load(data_dir / "quota.sqlite3")
            await self._draw_limiter.load(data_dir / "draw_limits.sqlite3")
            await self.proactive_state.load(data_dir / "proactive_images.json")
            self._dispatcher = CommandDispatcher(self)
            self._action_pipeline = build_action_pipeline()

            if self.config.selfie.auto_enabled:
                from .core.selfie.auto_selfie_task import AutoSelfieTask
                self._auto_selfie_task = AutoSelfieTask(self)
                await self._auto_selfie_task.start()

            if self.config.group_image.enabled:
                from .core.selfie.group_image_task import GroupImageTask
                self._group_image_task = GroupImageTask(self)
                await self._group_image_task.start()
        except BaseException:
            # 后续启动失败仍清理先前的任务和存储，不能留下孤立后台协程或数据库句柄。
            try:
                await self._close_resources()
            except BaseException:
                logger.exception("启动失败后的资源清理也出现异常")
            raise

        logger.info(f"麦麦绘卷已加载 (v{self.config.plugin.config_version}, Pipeline 架构)")

    async def on_unload(self) -> None:
        await self._close_resources()
        logger.info("麦麦绘卷已卸载")

    async def _close_resources(self) -> None:
        """宿主先 on_unload 再取消 RPC；插件必须自行排空，取消也不能打断清理。"""
        self._closing = True
        self._dispatcher = None
        self._action_pipeline = None
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._finish_cleanup(), name="mais-art:cleanup")
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._cleanup_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if self._cleanup_task.cancelled():
                    raise
        if cancelled:
            raise asyncio.CancelledError()

    async def _finish_cleanup(self) -> None:
        """即使某个后台任务停止失败，也关闭其他任务，并在请求结算之后关库。"""
        try:
            await self._trial_manager.close()
            # 热更新也在 active 中：必须等它结束，再最终停止后台，防止停完又被重启。
            # 此处在独立且被 shield 的清理任务内，不会被宿主重复取消打断。
            active = [task for task in self._active_operations if not task.done()]
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            recalls = list(self._recall_tasks)
            for task in recalls:
                task.cancel()
            if recalls:
                await asyncio.gather(*recalls, return_exceptions=True)
            self._recall_tasks.clear()
        finally:
            try:
                if self._group_image_task is not None:
                    await self._group_image_task.stop()
            finally:
                try:
                    if self._auto_selfie_task is not None:
                        await self._auto_selfie_task.stop()
                finally:
                    try:
                        await self.proactive_state.close()
                    finally:
                        try:
                            await self.quota_store.close()
                        finally:
                            try:
                                await self._draw_limiter.close()
                            finally:
                                await self.runtime_state.close()

    @_tracked_operation
    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return

        # 先取消旧周期，再读取新配置；保留冷却与消息游标，避免热更新重复发布。
        from .core.selfie.auto_selfie_task import AutoSelfieTask
        from .core.selfie.group_image_task import GroupImageTask
        if self._auto_selfie_task is not None:
            await self._auto_selfie_task.stop()
        if self._group_image_task is not None:
            await self._group_image_task.stop()
        if self._closing:
            return
        if self.config.selfie.auto_enabled:
            if self._auto_selfie_task is None:
                self._auto_selfie_task = AutoSelfieTask(self)
            await self._auto_selfie_task.start()
        if self.config.group_image.enabled:
            if self._group_image_task is None:
                self._group_image_task = GroupImageTask(self)
            try:
                await self._group_image_task.start()
            except BaseException:
                # 群图任务无法使用新配置时，也停止本次热更新刚创建的后台周期。
                await self._group_image_task.stop()
                if self._auto_selfie_task is not None:
                    await self._auto_selfie_task.stop()
                raise

    # ── WebUI: 配置与绘图 ──────────────────────────

    @API("trial_start", description="启动控制台绘图", version="1")
    @_tracked_operation
    async def trial_start(self, payload: str, request_id: str) -> Dict[str, Any]:
        return self._trial_manager.start(payload, request_id)

    @API("trial_status", description="查询控制台绘图状态", version="1")
    @_tracked_operation
    async def trial_status(self, job_id: str = "") -> Dict[str, Any]:
        return self._trial_manager.status(job_id)

    @API("trial_result", description="读取控制台绘图图片分片", version="1")
    @_tracked_operation
    async def trial_result(self, job_id: str, index: int) -> Dict[str, Any]:
        return self._trial_manager.chunk(job_id, index)

    @API("trial_cancel", description="停止控制台绘图等待", version="1")
    @_tracked_operation
    async def trial_cancel(self, job_id: str) -> Dict[str, Any]:
        return await self._trial_manager.cancel(job_id)

    @API("trial_remove_reference", description="移除绘图参考图", version="1")
    @_tracked_operation
    async def trial_remove_reference(self, reference_id: str) -> Dict[str, Any]:
        return self._trial_manager.remove_reference(reference_id)

    @API("trial_upload", description="上传绘图参考图", version="1")
    @_tracked_operation
    async def trial_upload(self, upload_id: str, filename: str = "") -> Dict[str, Any]:
        del filename
        source = None
        try:
            claimed = await self.ctx.call_capability("webui.claim_upload", upload_id=upload_id)
            candidate = Path(claimed["path"]).resolve()
            if not candidate.is_relative_to((self.ctx.paths.data_dir / "uploads").resolve()):
                raise ValueError("绘图上传文件路径不在插件上传目录内")
            source = candidate
            return await self._trial_manager.upload(source)
        except Exception:
            logger.exception("绘图参考图上传失败")
            return {"success": False, "error": "参考图无法读取，请上传不超过 20 MB 的 JPG、PNG 或 WebP 图片。"}
        finally:
            if source is not None:
                try:
                    await asyncio.to_thread(source.unlink, missing_ok=True)
                except OSError:
                    logger.warning("绘图上传临时文件未能清理", exc_info=True)

    @API("model_config_load", description="读取绘卷控制台配置数据", version="1")
    async def model_config_load(self) -> Dict[str, Any]:
        from .core.config.model_editor import load_editor
        return await asyncio.to_thread(load_editor, Path(self.plugin_dir) / "config.toml", self.build_config_schema())

    @API(
        "model_config_save", description="保存绘卷控制台配置数据", version="1",
        parameters={
            "payload": {"type": "string", "description": "模型列表、默认模型和控制台设置 JSON", "required": True},
            "revision": {"type": "string", "description": "读取时的配置版本摘要", "required": True},
        },
    )
    async def model_config_save(self, payload: str, revision: str) -> Dict[str, Any]:
        from .core.config.model_editor import save_editor
        return await asyncio.to_thread(save_editor, Path(self.plugin_dir) / "config.toml", payload, revision)

    async def _appearance_operation(self, operation: Callable[..., Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Any]:
        from .core.config.appearance import appearance_error
        try:
            return await asyncio.to_thread(operation, Path(self.plugin_dir) / "data" / "webui", *args, **kwargs)
        except Exception as error:
            logger.exception("绘卷页面外观操作失败")
            return appearance_error(error)

    @API("appearance_load", description="读取绘卷页面外观", version="1")
    async def appearance_load(self) -> Dict[str, Any]:
        from .core.config.appearance import load_appearance
        return await self._appearance_operation(load_appearance)

    @API("appearance_save", description="保存绘卷页面外观", version="1")
    async def appearance_save(self, payload: str) -> Dict[str, Any]:
        from .core.config.appearance import save_appearance
        return await self._appearance_operation(save_appearance, payload)

    @API("appearance_reset", description="恢复绘卷页面默认外观", version="1")
    async def appearance_reset(self) -> Dict[str, Any]:
        from .core.config.appearance import reset_appearance
        return await self._appearance_operation(reset_appearance)

    @API("appearance_remove_image", description="移除绘卷页面背景图片", version="1")
    async def appearance_remove_image(self) -> Dict[str, Any]:
        from .core.config.appearance import reset_appearance
        return await self._appearance_operation(reset_appearance, remove_image=True)

    @API("appearance_upload", description="上传绘卷页面背景图片", version="1")
    async def appearance_upload(self, upload_id: str, filename: str = "") -> Dict[str, Any]:
        from .core.config.appearance import appearance_error, upload_background
        source = None
        try:
            claimed = await self.ctx.call_capability("webui.claim_upload", upload_id=upload_id)
            candidate = Path(claimed["path"]).resolve()
            if not candidate.is_relative_to((self.ctx.paths.data_dir / "uploads").resolve()):
                raise ValueError("背景上传文件路径不在插件上传目录内")
            source = candidate
            return await self._appearance_operation(upload_background, source, filename)
        except Exception as error:
            logger.exception("绘卷背景图片上传失败")
            return appearance_error(error)
        finally:
            if source is not None:
                try:
                    await asyncio.to_thread(source.unlink, missing_ok=True)
                except OSError:
                    logger.warning("背景图片已处理，但上传临时文件未能清理", exc_info=True)

    @Tool(
        "draw_picture",
        description=_DRAW_TOOL_DESCRIPTION,
        parameters=_DRAW_TOOL_PARAMETERS,
        # 覆盖 ComfyUI 默认 600 秒轮询，以及完成后下载结果的余量；MaiBot
        # 未声明时会使用 60 秒的宿主默认值。
        timeout_ms=660000,
    )
    @_tracked_operation
    async def handle_draw_picture(self, **kwargs: Any):
        """智能生图入口（@Tool），LLM/规划器调用时由 host 把 function_args 平铺进 kwargs，
        同时附带 stream_id / chat_id / group_id / user_id / platform 上下文字段。
        所有逻辑委托给 action pipeline。
        """
        if not self.config.basic.enable_unified_generation:
            return False, "聊天智能生图已关闭"
        if self._action_pipeline is None:
            logger.error("Action pipeline 尚未初始化")
            return False, "插件未就绪"

        from .core.pipeline import build_request_from_tool_kwargs, make_pipeline_context

        stream_id = str(kwargs.get("stream_id") or "")
        if not self.runtime_state.is_plugin_enabled(stream_id, self.config.plugin.enabled):
            return False, "已跳过"
        limit = self.config.basic.max_draws_per_request
        token = None
        req = None
        try:
            message_id = await resolve_draw_message(self.ctx, stream_id, str(kwargs.get("msg_id") or ""))
            if limit:
                token = await self._draw_limiter.acquire(stream_id, message_id, limit)
                if token is None:
                    return False, f"本次用户请求最多生成 {limit} 张图片，已达到上限。不要继续调用生图工具；请等待用户新的画图要求。"
            req = await build_request_from_tool_kwargs(self, kwargs)
            if req is None:
                return False, "已跳过"
            ctx = make_pipeline_context(self, req)
            out = await self._action_pipeline.run(req, ctx)
            return out.success, out.user_message or out.error or ("生成成功" if out.success else "生成失败")
        except (ValueError, RuntimeError) as exc:
            logger.warning("智能生图已停止：%s", exc)
            return False, str(exc)
        finally:
            if token is not None and (
                req is None or (not req.generation_started and req.delivery_state == "not_sent" and not req.success)
            ):
                await self._draw_limiter.release(token)

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
        timeout_ms=660000,
    )
    @_tracked_operation
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
        activity_type: str = "",
        activity_description: str = "",
        outfit: str = "",
        activity_location: str = "",
        activity_time_window: str = "",
        activity_as_of: str = "",
        activity_timezone: str = "",
        activity_scene: str = "",
        recent_selfie_styles: Optional[List[str]] = None,
        include_photo_plan: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """对外 API：生成图片，返回 base64

        Args:
            prompt: 图片描述（必填，中英文均可；优化器按后端输出英文自然语言或标签）
            model_id: 模型 ID（如 model1、model2），留空时按文生图/图生图场景自动选择默认模型
            size: 图片尺寸，如 "1024x1024"；留空由 LLM 自选或用模型默认
            strength: 图生图强度 0.1-1.0；仅 input_image_base64 非空时生效，默认 0.7
            input_image_base64: 图生图输入图（纯 base64 字符串，不含 data:image/... 前缀）
            negative_prompt: 额外负面提示词（合并到模型自身的 negative_prompt_add）
            selfie_mode: 是否启用自拍模式（默认 false）
            selfie_style: 自拍风格 auto/standard/mirror/photo（仅 selfie_mode=true 生效）
            free_hand_action: 自由手部动作（英文，仅 selfie_mode=true 生效）
            use_cache: 是否参与缓存读写（默认 false，每次都重新生成）
            activity_type: 本次活动类型；外部生图 API 始终使用调用方的活动，不回调 MaiTrace
            activity_description: 本次活动描述，用于场景与换装
            outfit: 显式服装，优先于活动默认服装
            activity_location: 当前地点；与服装、动作保持一致
            activity_time_window: 当前活动时间窗
            activity_as_of: 日程快照对应的 ISO 时间
            activity_timezone: 日程时区
            activity_scene: MaiTrace 当前细化场景
            recent_selfie_styles: 近期自拍构图，auto 避免重复
            include_photo_plan: 成功时附加本次实际自拍方案；默认 false 保留旧 API 返回字段

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
        if kwargs:
            raise TypeError(f"未支持的生图参数: {', '.join(sorted(kwargs))}")

        if not prompt or not str(prompt).strip():
            return _generation_failure("prompt 不能为空")

        # 参数标准化；不把错误参数静默改成另一套生成要求。
        strength_f: Optional[float] = None
        if strength is not None:
            try:
                if isinstance(strength, bool):
                    raise ValueError("strength 必须是数值，不能是布尔值")
                strength_f = float(strength)
                if not (0.1 <= strength_f <= 1.0):
                    raise ValueError("strength 必须在 0.1 到 1.0 之间")
            except (ValueError, TypeError) as exc:
                return _generation_failure(str(exc))

        style = selfie_style.strip().lower()
        if style not in ("auto", "standard", "mirror", "photo"):
            return _generation_failure(f"未知自拍风格: {style}")

        from .core.selfie.reference_image import load_reference_image
        from .core.selfie.schedule_provider import ActivityType
        if activity_type and activity_type not in {item.value for item in ActivityType}:
            return _generation_failure(f"未知活动类型: {activity_type}")
        if recent_selfie_styles is not None and (
            not isinstance(recent_selfie_styles, list)
            or any(item not in {"standard", "mirror", "photo"} for item in recent_selfie_styles)
        ):
            return _generation_failure("recent_selfie_styles 必须为有效构图列表")
        if selfie_mode and not input_image_base64:
            input_image_base64 = load_reference_image(self.plugin_dir, self.config.selfie.reference_image_path)

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
            activity_type=activity_type,
            activity_description=activity_description.strip(),
            outfit=outfit.strip(),
            activity_location=activity_location.strip(),
            activity_time_window=activity_time_window.strip(),
            activity_as_of=activity_as_of.strip(),
            activity_timezone=activity_timezone.strip(),
            activity_scene_description=activity_scene.strip(),
            recent_selfie_styles=(recent_selfie_styles or [])[-6:],
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

        result = {
            "success": bool(out.success),
            "image_base64": out.resolved_image_data or "",
            "model_id": req.model_id,
            "size": req.final_size or req.size,
            "is_img2img": bool(req.is_img2img),
            "error": out.error or "",
            "selfie_style": req.selfie_style if req.is_selfie else "",
            "outfit": req.outfit if req.is_selfie else "",
        }
        if include_photo_plan and out.success and req.is_selfie:
            result["photo_plan"] = req.extras.get("photo_plan", {})
        return result

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
        timeout_ms=660000,
        description="麦麦绘卷命令套件（生图 / 模型管理 / 风格管理 / 自拍开关 / 额度）。命令前缀保存后重新加载插件生效",
        pattern=r"^\s*(?:\[(?=[^\]]*(?:图片|image|表情|emoji))(?:[^\[\]]|\[[^\]]*\])*\]\s*)*(?:.*，说：\s*)?/dr(?:\s+(?P<rest>.+))?$",
    )
    @_tracked_operation
    async def handle_dr(self, **kwargs: Any):
        """命令入口：实际 pattern 由 get_components() 按 config.basic.command_prefix 动态注入

        装饰器里的 pattern 仅是默认占位（/dr）。get_components() 返回 host 之前会按
        当前 config.basic.command_prefix 重写 metadata.command_pattern 为精确前缀，
        避免抢占其他插件的命令（如 /mute on）。修改前缀后重新加载插件即生效。
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
        platform = str(kwargs.get("platform") or "")

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
            platform=platform,
            user_nickname=user_nickname,
            group_name=group_name,
        )

    # ── get_components 覆写：动态注入用户配置的精确命令前缀 ──

    def get_components(self) -> List[Dict[str, Any]]:
        """注入 basic.command_prefix 到 dr command 的 pattern，避免宽匹配抢占其他插件"""
        components = super().get_components()
        if not self.get_config("basic.enable_unified_generation", True):
            components = [item for item in components if item.get("name") != "draw_picture"]

        # SDK 可能在配置注入前查询组件；公开配置快照为空时使用默认前缀。
        prefix = str(self.get_config("basic.command_prefix", "/dr") or "/dr").strip()
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
    """SDK Runner 工厂入口：先迁移磁盘配置，避免宿主重建时丢弃旧字段。"""
    from .core.config.migration import upgrade_config_file
    plugin = MaisArtPlugin()
    upgrade_config_file(Path(plugin.plugin_dir) / "config.toml")
    return plugin


def _extract_user_text_segments(message: Optional[Dict[str, Any]]) -> tuple[str, bool]:
    """从当前 SDK 的 raw_message 段提取用户净输入文本及引用标记。

    只拼接 text 段，跳过 reply、image、emoji、at 等段，避免引用别人的命令后误触发。
    """
    if message is None:
        return "", False

    parts: list[str] = []
    has_reply = bool(message.get("reply_to"))
    for segment in message.get("raw_message") or []:
        if segment["type"] == "reply":
            has_reply = True
        elif segment["type"] == "text":
            parts.append(segment["data"])
    return "".join(parts).strip(), has_reply


def _extract_display_names(message: Optional[Dict[str, Any]]) -> tuple[str, str]:
    """从当前 SDK 消息的 message_info 提取昵称和群名，用于日志前缀。"""
    if message is None:
        return "", ""

    msg_info = message.get("message_info") or {}
    user_info = msg_info.get("user_info") or {}
    group_info = msg_info.get("group_info") or {}
    return (
        str(user_info.get("user_nickname") or user_info.get("user_cardname") or ""),
        str(group_info.get("group_name") or ""),
    )


def _looks_like_quoted_command(message: Optional[Dict[str, Any]], configured_prefix: str) -> bool:
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
