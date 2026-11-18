"""按 SDK 发送回执中的平台消息 ID 调度撤回。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

import asyncio
import json
import logging
import re
import time

if TYPE_CHECKING:
    from maibot_sdk import PluginContext


logger = logging.getLogger("plugin.mais_art_journal.recall")


@dataclass(frozen=True)
class RecallTarget:
    api_name: str
    args: Dict[str, Any]
    via_maibot: bool = False


async def _resolve_official_recall(
    ctx: "PluginContext", chat_id: str, message_id: str, log_prefix: str,
) -> Optional[RecallTarget]:
    """确认当前 QQ 消息与官方工具签名；通过麦麦处理，不直接执行工具。"""
    message = await ctx.message.get_by_id(
        message_id=message_id, chat_id=chat_id, include_binary_data=False,
    )
    if (
        not isinstance(message, dict) or message.get("platform") != "qq"
        or message.get("message_id") != message_id or message.get("session_id") != chat_id
    ):
        logger.warning("%s 未确认当前聊天中的 QQ 官方消息，本次跳过，message_id=%s", log_prefix, message_id)
        return None
    tools = await ctx.call_capability("tool.get_definitions")
    if not isinstance(tools, list):
        raise RuntimeError("宿主工具列表结构无效")
    matching = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") != "recall_message":
            continue
        definition = tool.get("definition", {})
        schema = definition.get("parameters_schema", {}) if isinstance(definition, dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if (
            isinstance(properties, dict) and isinstance(properties.get("msg_id"), dict)
            and properties["msg_id"].get("type") == "string"
        ):
            matching.append(tool)
    if len(matching) != 1:
        logger.warning("%s 未找到唯一且已启用的 QQ 官方撤回工具，本次跳过，message_id=%s", log_prefix, message_id)
        return None
    return RecallTarget("", {"msg_id": message_id}, via_maibot=True)


async def _resolve_recall_target(
    ctx: "PluginContext", message_id: str, log_prefix: str,
    *, chat_id: str = "",
) -> Optional[RecallTarget]:
    """OneBot 使用公开 API；官方字符串回执交给麦麦的现有撤回工具。"""
    if re.fullmatch(r"-?[0-9]+", message_id) is None:
        return await _resolve_official_recall(ctx, chat_id, message_id, log_prefix)
    if int(message_id) == 0:
        logger.warning("%s 平台消息 ID 无效，本次跳过，message_id=%s", log_prefix, message_id)
        return None

    apis = await ctx.api.list()
    if not isinstance(apis, list):
        raise RuntimeError("宿主未返回可用的适配器 API 列表")

    def find_api(name: str) -> str:
        matches = [
            entry for entry in apis
            if isinstance(entry, dict) and entry.get("name") == name
            and entry.get("version") == "1" and entry.get("enabled") is True
        ]
        if len(matches) > 1:
            raise RuntimeError(f"撤回 API {name} 有多个提供方，无法确认发送账号")
        if not matches:
            return ""
        entry = matches[0]
        return str(entry.get("full_name") or f"{entry['plugin_id']}.{name}")

    # 两个前缀在当前统一 QQ 适配器中是同一处理器的别名，不切换客户端类型。
    for name in ("adapter.snowluma.message.delete_msg", "adapter.napcat.message.delete_msg"):
        api_name = find_api(name)
        if api_name:
            return RecallTarget(api_name, {"message_id": message_id})
    logger.warning("%s 未找到本次发送消息可用的撤回接口，本次跳过，message_id=%s", log_prefix, message_id)
    return None


async def schedule_auto_recall(
    ctx: "PluginContext", chat_id: str, message_id: str,
    delay_seconds: float, log_prefix: str,
) -> Optional[asyncio.Task]:
    """只撤回本次发送返回的真实消息 ID；返回任务，由插件负责卸载时取消。"""
    if delay_seconds <= 0 or not chat_id or not message_id:
        return None
    scheduled_at = time.monotonic()
    try:
        target = await _resolve_recall_target(ctx, message_id, log_prefix, chat_id=chat_id)
    except Exception:
        logger.exception("%s 确认撤回接口失败，message_id=%s", log_prefix, message_id)
        return None
    if target is None:
        return None
    if target.via_maibot and delay_seconds >= 120:
        logger.warning("%s QQ 官方消息超过 2 分钟不可撤回，当前延时 %s 秒，本次跳过", log_prefix, delay_seconds)
        return None

    async def recall() -> None:
        # SDK 查询耗时计入配置延时，不能再在查询完成后等待一个完整周期。
        remaining_delay = max(0.0, delay_seconds - (time.monotonic() - scheduled_at))
        await asyncio.sleep(remaining_delay)
        try:
            if target.via_maibot:
                if time.monotonic() - scheduled_at >= 120:
                    logger.warning("%s QQ 官方撤回任务已超过 2 分钟，本次跳过，message_id=%s", log_prefix, message_id)
                    return
                call = json.dumps({"tool": "recall_message", "arguments": target.args}, ensure_ascii=False)
                result = await ctx.call_capability(
                    "maisaka.proactive.trigger", stream_id=chat_id,
                    intent=(
                        "麦麦绘卷请求撤回本插件刚才在当前聊天中发送的图片，已到用户配置的撤回时间。"
                        f"请调用当前 QQ 官方撤回工具，调用参数为：{call}。"
                        "msg_id 必须使用这里提供的完整平台消息 ID；不要改用最近消息或撤回其他消息。"
                        "若工具不可用或平台不允许撤回，请结束本轮。处理后无需发送聊天回复。"
                    ),
                    reason="绘卷图片自动撤回时间已到",
                    metadata={"task_type": "image_auto_recall", "message_id": message_id, "stream_id": chat_id},
                )
                if isinstance(result, dict) and result.get("success") is True and result.get("queued") is True:
                    logger.info("%s 已向麦麦提交 QQ 官方撤回任务，message_id=%s task_id=%s；尚未确认撤回结果", log_prefix, message_id, result.get("task_id", ""))
                else:
                    logger.warning("%s 麦麦未接受 QQ 官方撤回任务，message_id=%s，回执=%r", log_prefix, message_id, result)
                return
            result = await ctx.api.call(
                target.api_name, version="1", **target.args,
            )
            if (
                isinstance(result, dict) and result.get("status") == "ok"
                and result.get("retcode", 0) == 0 and result.get("success") is not False
            ):
                logger.info("%s 图片已撤回，message_id=%s", log_prefix, message_id)
            else:
                logger.warning("%s 撤回未成功，message_id=%s，回执=%r", log_prefix, message_id, result)
        except Exception:
            logger.exception("%s 撤回 API 调用异常，message_id=%s", log_prefix, message_id)

    return asyncio.create_task(recall(), name=f"mais-art:recall:{message_id}")
