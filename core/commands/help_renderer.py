"""help / config 等渲染入口"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import get_model_config, resolve_img2img_model, resolve_txt2img_model
from ..state import runtime_state
from .registry import CommandResult

if TYPE_CHECKING:
    from .dispatcher import DispatcherContext

logger = logging.getLogger("plugin.mais_art_journal.help")


async def render_help(dctx: "DispatcherContext") -> CommandResult:
    """渲染 help / 空调用的帮助文案（前缀跟随 basic.command_prefix）"""
    prefix = dctx.prefix
    lines = [
        "🎨 图片风格系统帮助\n",
        "📋 基本命令：",
        f"• {prefix} <风格名> - 对最近的图片应用风格",
        f"• {prefix} <描述> - 自然语言生成图片",
        f"• {prefix} styles - 列出所有可用风格",
        f"• {prefix} list - 查看所有模型",
        f"• {prefix} config - 查看当前配置",
    ]
    if dctx.is_admin:
        lines.extend([
            "\n⚙️ 管理员命令：",
            f"• {prefix} on|off - 开关插件",
            f"• {prefix} model on|off <模型ID> - 开关模型",
            f"• {prefix} recall on|off <模型ID> - 开关撤回",
            f"• {prefix} selfie on|off - 开关自拍日程增强",
            f"• {prefix} selfie standard|mirror|photo - 切换自拍风格",
            f"• {prefix} default <模型ID> - 设置默认模型",
            f"• {prefix} set <模型ID> - 设置命令模型",
            f"• {prefix} style <风格名> - 查看风格详情",
            f"• {prefix} reset - 重置所有配置",
        ])
    lines.extend([
        "\n💡 使用流程：",
        "1. 发送一张图片",
        f"2. 使用 {prefix} <风格名> 进行风格转换",
        "3. 等待处理完成",
    ])

    await dctx.plugin.ctx.send.text("\n".join(lines), dctx.stream_id)
    return True, "帮助信息显示成功", True


async def render_current_config(dctx: "DispatcherContext") -> CommandResult:
    """config：渲染当前聊天流的配置摘要"""
    plugin = dctx.plugin
    chat_id = dctx.chat_id
    prefix = dctx.prefix

    global_txt2img = resolve_txt2img_model(plugin)
    global_img2img = resolve_img2img_model(plugin)
    global_command_model = plugin.config.basic.pic_command_model
    global_plugin_enabled = plugin.config.plugin.enabled

    plugin_enabled = runtime_state.is_plugin_enabled(chat_id, global_plugin_enabled)
    # 聊天流级覆盖优先；未覆盖时展示按模式拆分的全局默认
    action_override = runtime_state.get_action_default_model(chat_id, "")
    txt2img_model = action_override or global_txt2img
    img2img_model = action_override or global_img2img
    command_model = runtime_state.get_command_default_model(chat_id, global_command_model)
    disabled_models = runtime_state.get_disabled_models(chat_id)
    recall_disabled = runtime_state.get_recall_disabled_models(chat_id)

    global_schedule = plugin.config.selfie.schedule_enabled
    selfie_schedule = runtime_state.is_selfie_schedule_enabled(chat_id, global_schedule)
    global_style = plugin.config.selfie.default_style
    selfie_style = runtime_state.get_selfie_style(chat_id, global_style)

    txt2img_cfg = get_model_config(plugin, txt2img_model)
    img2img_cfg = get_model_config(plugin, img2img_model)
    command_cfg = get_model_config(plugin, command_model)

    def _name(cfg):
        if not isinstance(cfg, dict):
            return "未知"
        return cfg.get("name", cfg.get("model", "未知"))

    lines = [
        f"⚙️ 当前聊天流配置 (ID: {chat_id[:8]}...)：\n",
        f"🔌 插件状态: {'✅ 启用' if plugin_enabled else '❌ 禁用'}",
        f"⌨️ 命令前缀: {prefix}",
    ]
    if action_override:
        lines.append(f"🎯 默认模型(聊天流覆盖): {action_override}")
        lines.append(f"   • 名称: {_name(txt2img_cfg)}\n")
    else:
        lines.append(f"🎯 文生图默认: {txt2img_model}")
        lines.append(f"   • 名称: {_name(txt2img_cfg)}")
        lines.append(f"🖼️ 图生图默认: {img2img_model}")
        lines.append(f"   • 名称: {_name(img2img_cfg)}\n")
    lines += [
        f"🔧 {prefix}命令模型: {command_model}",
        f"   • 名称: {_name(command_cfg)}",
        f"\n📸 自拍日程增强: {'✅ 启用' if selfie_schedule else '❌ 禁用'}",
        f"📷 自拍风格: {selfie_style}",
    ]
    if disabled_models:
        lines.append(f"\n❌ 已禁用模型: {', '.join(disabled_models)}")
    if recall_disabled:
        lines.append(f"🔕 撤回已关闭: {', '.join(recall_disabled)}")

    await dctx.plugin.ctx.send.text("\n".join(lines), dctx.stream_id)
    return True, "配置信息查询成功", True
