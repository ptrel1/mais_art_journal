"""/dr 配置管理子命令：list / models / config / set / default / reset"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...config import (
    get_model_config,
    list_models,
    model_exists,
    resolve_img2img_model,
    resolve_txt2img_model,
)
from ...state import runtime_state
from ..help_renderer import render_current_config
from ..registry import CommandResult, subcommand

if TYPE_CHECKING:
    from ...plugin import MaisArtPlugin
    from ..dispatcher import DispatcherContext

logger = logging.getLogger("plugin.mais_art_journal.cmd.config")


@subcommand("list", aliases=("models",), admin=False)
async def cmd_list(plugin: "MaisArtPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """/dr list ｜ /dr models — 列出所有模型（管理员可见禁用模型）"""
    models = list_models(plugin)
    if not models:
        await plugin.ctx.send.text("未找到任何模型配置", dctx.stream_id)
        return False, "无模型配置", True

    global_txt2img = resolve_txt2img_model(plugin)
    global_img2img = resolve_img2img_model(plugin)
    global_command = plugin.config.basic.pic_command_model

    runtime_override = runtime_state.get_action_default_model(dctx.chat_id, "")
    command_default = runtime_state.get_command_default_model(dctx.chat_id, global_command)
    disabled_models = runtime_state.get_disabled_models(dctx.chat_id)
    recall_disabled = runtime_state.get_recall_disabled_models(dctx.chat_id)

    lines = ["📋 可用模型列表：\n"]
    for model_id, cfg in models.items():
        is_disabled = model_id in disabled_models
        if is_disabled and not dctx.is_admin:
            continue

        model_name = cfg.get("name", cfg.get("model", "未知"))
        support_img2img = cfg.get("support_img2img", True)
        # 没有运行时覆盖时，✅ 标记同时落在文生图/图生图默认模型上
        txt_mark = " ✅" if not runtime_override and model_id == global_txt2img else ""
        img_mark = " 🖼️" if not runtime_override and model_id == global_img2img else ""
        command_mark = " 🔧" if model_id == command_default else ""
        img2img_mark = " 🎨" if support_img2img else " 📝"
        disabled_mark = " ❌" if is_disabled else ""
        recall_mark = " 🔕" if model_id in recall_disabled else ""

        lines.append(
            f"• {model_id}{txt_mark}{img_mark}{command_mark}{img2img_mark}{disabled_mark}{recall_mark}\n"
            f"  模型: {model_name}\n"
        )
    if runtime_override:
        lines.append(f"\n⌨️ 当前聊天流已覆盖默认模型：{runtime_override}")
    lines.append(
        f"\n📖 图例：✅文生图默认 🖼️图生图默认 🔧{dctx.prefix}命令 🎨支持图生图 📝仅文生图"
    )
    await plugin.ctx.send.text("\n".join(lines), dctx.stream_id)
    return True, "模型列表查询成功", True


@subcommand("config", admin=True)
async def cmd_config(plugin: "MaisArtPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """/dr config — 显示当前聊天流配置"""
    return await render_current_config(dctx)


@subcommand("set", admin=True)
async def cmd_set(plugin: "MaisArtPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """/dr set <模型ID> — 设置 /dr 命令使用的模型"""
    model_id = args.strip()
    if not model_id:
        await plugin.ctx.send.text(f"请指定模型ID，格式：{dctx.prefix} set <模型ID>", dctx.stream_id)
        return False, "缺少模型ID参数", True

    if not model_exists(plugin, model_id):
        await plugin.ctx.send.text(
            f"模型 '{model_id}' 不存在，请使用 {dctx.prefix} list 查看可用模型", dctx.stream_id,
        )
        return False, f"模型 '{model_id}' 不存在", True

    if not runtime_state.is_model_enabled(dctx.chat_id, model_id):
        await plugin.ctx.send.text(f"模型 '{model_id}' 已被禁用", dctx.stream_id)
        return False, f"模型 '{model_id}' 已被禁用", True

    runtime_state.set_command_default_model(dctx.chat_id, model_id)
    await plugin.ctx.send.text(f"已切换: {model_id}", dctx.stream_id)
    return True, f"模型切换成功: {model_id}", True


@subcommand("default", admin=True)
async def cmd_default(plugin: "MaisArtPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """/dr default <模型ID> — 设置 Action 组件默认模型"""
    model_id = args.strip()
    if not model_id:
        await plugin.ctx.send.text(f"格式：{dctx.prefix} default <模型ID>", dctx.stream_id)
        return False, "缺少模型ID", True

    if not model_exists(plugin, model_id):
        await plugin.ctx.send.text(f"模型 '{model_id}' 不存在", dctx.stream_id)
        return False, "模型不存在", True

    if not runtime_state.is_model_enabled(dctx.chat_id, model_id):
        await plugin.ctx.send.text(f"模型 '{model_id}' 已被禁用", dctx.stream_id)
        return False, "模型已被禁用", True

    runtime_state.set_action_default_model(dctx.chat_id, model_id)
    await plugin.ctx.send.text(f"已设置: {model_id}", dctx.stream_id)
    return True, "设置成功", True


@subcommand("reset", admin=True)
async def cmd_reset(plugin: "MaisArtPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """/dr reset — 重置当前聊天流的所有运行时配置"""
    runtime_state.reset_chat_state(dctx.chat_id)
    global_txt2img = resolve_txt2img_model(plugin)
    global_img2img = resolve_img2img_model(plugin)
    global_command_model = plugin.config.basic.pic_command_model

    await plugin.ctx.send.text(
        f"✅ 当前聊天流配置已重置！\n\n"
        f"🎯 文生图默认: {global_txt2img}\n"
        f"🖼️ 图生图默认: {global_img2img}\n"
        f"🔧 {dctx.prefix}命令模型: {global_command_model}\n"
        f"📋 所有模型已启用\n"
        f"🔔 所有撤回已启用\n\n"
        f"使用 {dctx.prefix} config 查看当前配置",
        dctx.stream_id,
    )
    logger.info(f"聊天流 {dctx.chat_id} 配置已重置")
    return True, "配置重置成功", True
