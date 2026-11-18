"""按首次配置流程组织 WebUI；展示分区仍指向原有配置路径。"""

from typing import Any, Dict, List

from .models import ActivityRule, ModelConfig
from .quota import QuotaRule


# 用户明确要求保留这些术语；这里只补充用途和填写示例。
FIELD_TEXT = {
    "plugin.enabled": ("启用插件", "启用并保存后，WebUI 侧栏会出现「绘卷控制台」，可管理模型、风格、自拍与次数设置。"),
    "basic.default_txt2img_model": ("默认文生图模型", "填写绘图服务的配置编号，例如 model1；留空使用 model1。"),
    "basic.default_img2img_model": ("默认图生图模型", "填写支持修改图片的服务编号，例如 model2；留空沿用默认文生图模型。"),
    "basic.prompt_optimizer_enabled": ("提示词优化器", "自动整理画图描述和自拍场景，建议开启；关闭后尺寸判断和配文等仍会使用辅助模型。"),
    "basic.command_prefix": ("画图命令", "默认 /dr，例如 /dr 画一只猫；修改后重启插件生效。"),
    "basic.enable_unified_generation": ("允许聊天请求画图", "开启后，可以直接对麦麦说“帮我画一只猫”；修改后重新加载插件以更新工具入口。"),
    "basic.enable_pic_command": ("允许用命令画图", "开启后，可以使用 /dr 画图；关闭后查询和管理命令仍可用。"),
    "basic.pic_command_model": ("命令画图使用的模型", "填写服务的配置编号，例如 model1，请勿留空；当前聊天通过 /dr set 设置的模型优先。"),
    "basic.admin_users": ("可以管理绘卷的用户", "逐项填写用户 ID；只有这些用户能切换设置、调整额度。官方 QQ 使用已绑定 ID 或 OpenID。"),
    "basic.max_retries": ("失败后最多重试几次", "不含第一次请求，0 表示不重试；密钥或权限错误不会重试。"),
    "basic.enable_debug_info": ("在聊天中显示生成进度", "显示开始生成、使用的模型和生成完成等提示。"),
    "basic.enable_verbose_debug": ("记录详细请求日志", "排查问题时再开启；支持 OpenAI、OpenAI-Chat 和百炼。"),
    "basic.cache_enabled": ("复用相同请求的图片", "相同画图请求可使用已有结果；关闭后每次重新生成。"),
    "basic.cache_max_size": ("最多缓存几张图片", "文生图、图生图分别按此数量缓存，重启后清空。"),
    "basic.auto_recall_enabled": ("自动撤回聊天图片", "开启后还需设置各模型的撤回时间；QQ 官方由麦麦调用撤回工具，延时须小于 120 秒并预留模型处理时间。"),
    "basic.llm_task_name": ("辅助模型使用的任务", "通常保留 utils；使用 MaiBot 已配置的模型任务，负责优化描述、选尺寸和配文。"),
    "basic.llm_timeout_seconds": ("辅助模型最多等待多久（秒）", "每次整理描述、判断场景或生成配文的等待上限。"),
    "basic.generation_timeout_seconds": ("一次生图最多等待多久（秒）", "包括整理描述、请求图片和重试；普通服务默认 170 秒，YesNovelAI 建议 420 秒。"),
    "basic.proactive_min_energy": ("主动发图所需的最低精力", "使用 MaiTrace 的 0–100 精力值，低于此值不主动发图。"),
    "selfie.enabled": ("允许生成角色自拍", "聊天自拍、自动自拍和 MaiTrace 自拍共用此开关。"),
    "selfie.reference_image_path": ("角色参考图文件", "填写图片文件路径，例如 images/reference.png；留空通过文字描述生成。"),
    "selfie.prompt_prefix": ("角色长什么样", "填写发型、发色、瞳色等固定特征；中文或英文均可，衣服和地点由活动决定。"),
    "selfie.negative_prompt": ("负面提示词", "填写自拍中要避免的内容；会与模型的负面提示词合并。"),
    "selfie.default_style": ("默认拍照方式", "建议选自动选择；也可以固定为前置自拍、对镜自拍或他人拍摄。"),
    "selfie.schedule_enabled": ("聊天自拍参考当前日程", "根据 MaiTrace 日程安排场景和服装；未安装 MaiTrace 时可关闭。"),
    "selfie.llm_selfie_model": ("聊天自拍使用的模型", "留空按是否有参考图选择默认模型；自动自拍未指定模型时也会沿用。"),
    "selfie.auto_enabled": ("主动分享日程自拍", "需要 MaiTrace 的有效日程；根据活动决定发空间、发群或跳过本轮。"),
    "selfie.interval_minutes": ("两次自拍至少间隔多久（分钟）", "空间和群共享这个间隔；生成失败或发送结果未知时也会等待。"),
    "selfie.check_interval_minutes": ("多久检查一次日程（分钟）", "检查时会判断是否适合拍照；每次检查不一定发图。"),
    "selfie.qzone_enabled": ("允许分享到 QQ 空间", "需要先在 MaiTrace 中配置空间发布。"),
    "selfie.group_enabled": ("允许分享到群聊", "使用下方允许群名单；无需开启群话题图片。"),
    "selfie.probability": ("适合分享时的发送概率（0–1）", "1 表示每次通过判断都分享，0.5 表示约一半，0 表示暂停。"),
    "selfie.selfie_model": ("主动自拍使用的模型", "留空沿用聊天自拍模型，再按参考图选择默认模型。"),
    "selfie.quiet_hours_start": ("自拍免打扰开始时间", "使用电脑本地时间，例如 00:00；与结束时间相同表示全天允许。"),
    "selfie.quiet_hours_end": ("自拍免打扰结束时间", "使用电脑本地时间，例如 07:00；这段时间不主动分享自拍。"),
    "selfie.caption_enabled": ("自动生成自拍配文", "根据生成的照片写配文；关闭后使用固定配文，群话题中的角色照片也遵守此设置。"),
    "selfie.caption_text": ("固定配文", "关闭自动配文时使用，例如“记录一下此刻。”"),
    "selfie.cycle_timeout_seconds": ("一次主动自拍最多等待多久（秒）", "包括查日程、生成图片、配文和发送。"),
    "selfie.disabled_activity_types": ("哪些活动不分享自拍", "睡眠、洗澡等活动默认禁止；精力不足或专注活动也会跳过。"),
    "selfie.activity_rules": ("不同活动的服装与拍照方式", "可分别设置工作、休闲等活动的衣服；已有日程中的明确服装优先。"),
    "group_image.enabled": ("根据群话题主动发图", "麦麦可根据最近聊天主动分享图片；需要 MaiTrace 日程，不控制日程自拍开关。"),
    "group_image.platform": ("群聊所在平台", "通常保留 qq；填写适配器实际使用的平台标识。"),
    "group_image.group_ids": ("允许主动发图的群", "逐项填写群 ID，留空不发群；日程自拍和群话题图片共用此名单。"),
    "group_image.interval_minutes": ("多久检查一次群话题（分钟）", "检查时会判断是否适合分享；每次检查不一定发图。"),
    "group_image.cooldown_minutes": ("同一群两次发图至少间隔多久（分钟）", "群话题图片和日程自拍共用此间隔。"),
    "group_image.probability": ("适合群话题时的发送概率（0–1）", "默认 0.15；只在话题适合分享时随机决定，不影响日程自拍。"),
    "group_image.min_messages": ("至少有几条新消息才判断", "群里积累到这些未处理的新消息后，才判断群话题图片。"),
    "group_image.context_messages": ("最多参考几条最近消息", "两类主动分享共用，须不少于最少新消息数。"),
    "group_image.context_minutes": ("参考最近多久的聊天（分钟）", "两类主动分享只参考这段时间内的有效消息。"),
    "group_image.quiet_hours_start": ("群聊免打扰开始时间", "使用电脑本地时间；群话题图片和日程自拍发群都遵守。"),
    "group_image.quiet_hours_end": ("群聊免打扰结束时间", "例如 07:00；与开始时间相同表示全天允许。"),
    "group_image.model_id": ("群话题图片使用的模型", "留空自动选择；主动自拍仍使用自己的模型设置。"),
    "group_image.disabled_activity_types": ("哪些活动不发群话题图片", "仅控制群话题图片，角色照片还遵守自拍活动限制。"),
    "group_image.cycle_timeout_seconds": ("一次群话题图片最多等待多久（秒）", "包括看话题、查日程、生成和发送。"),
    "quota.enabled": ("限制聊天生图次数", "限制用户请求的图片；主动分享和 MaiTrace 配图不占用此次数。"),
    "quota.period": ("次数多久重置", "每天、每周一或每月一日重置；一次性总额不会自动重置。"),
    "quota.timezone": ("重置时间使用的时区", "国内通常保留 Asia/Shanghai。"),
    "quota.group_limit": ("每个群最多生成几张", "整个群共享，每个周期重新计算；0 表示禁止群内生图。"),
    "quota.private_limit": ("每个私聊用户最多生成几张", "每人单独计算，每个周期重新计算；0 表示禁止私聊生图。"),
    "quota.rules": ("为指定群或用户单独设置次数", "留空使用上方默认次数；同一群或用户只添加一条规则。"),
    "models.items": ("绘图服务列表", "先填写服务类型、地址、密钥和服务商模型名称，再选择默认模型；编号用于命令引用。"),
    "styles.items": ("画风预设列表", "添加常用画风后，可使用 /dr 水彩 等命令；名称和别名都可以触发。"),
}

MODEL_TEXT = {
    "id": ("配置编号（留空自动编号）", "保存时为未填写的编号自动分配 model1、model2 等；手动填写时不能重复。"),
    "name": ("配置名称", "给这套绘图服务起一个名字，例如“我的 NovelAI”。"),
    "format": ("绘图服务类型", "按服务商提供的接口选择；OpenAI 兼容用于大多数中转服务。"),
    "base_url": ("服务地址", "从服务商复制 API 地址；OpenAI 兼容通常以 /v1 结尾。"),
    "api_key": ("API密钥", "从服务商复制密钥即可；OpenAI 兼容会自动补 Bearer，ComfyUI 可留空。"),
    "model": ("服务商提供的模型名称", "照服务商文档填写；ComfyUI 填工作流 JSON 文件路径，梦羽填模型数字索引。"),
    "fixed_size_enabled": ("始终使用固定尺寸", "开启后不再根据请求选择尺寸，使用下方默认尺寸。"),
    "default_size": ("默认图片尺寸", "通常填 1024x1024；Gemini 可填 16:9-2K。"),
    "seed": ("随机种子", "保留 -1 表示每次随机；固定值用于尝试复现图片。"),
    "guidance_scale": ("描述对画面的影响强度", "扩散模型使用；保持默认即可，OpenAI 兼容可填 -1 不发送此参数。"),
    "num_inference_steps": ("生成步数", "扩散模型使用；建议先保持默认，OpenAI 兼容可填 -1 不发送。"),
    "watermark": ("添加服务商水印", "只在豆包、百炼及火山方舟支持时生效，不能去除强制水印。"),
    "custom_prompt_add": ("正面增强词", "每张图都追加的描述或画风；留空不添加固定画风。"),
    "negative_prompt_add": ("负面提示词", "填写不希望出现的内容，例如模糊、错误的手部。"),
    "support_img2img": ("此服务支持修改已有图片", "请按服务能力设置；关闭后只使用文字生图。"),
    "prompt_syntax": ("提示词使用的格式", "一般保留自动选择；扩散中转服务可选择标签。"),
    "selfie_reference_strength": ("参考图自拍的改动强度", "0.1–1，越大通常改变越多；用于支持降噪强度的服务。"),
    "auto_recall_delay": ("图片发送多久后撤回（秒）", "0 表示不撤回；还需开启聊天图片撤回总开关。QQ 官方须小于 120 秒，并为麦麦处理预留时间。"),
    "cfg": ("CFG Rescale（通常保持默认）", "NovelAI、砂糖云的高级画面参数。"),
    "sampler": ("采样方式（通常保持默认）", "NovelAI、砂糖云使用；不确定时保持默认。"),
    "nocache": ("禁用砂糖云缓存（0 关 / 1 开）", "只控制砂糖云服务端缓存。"),
    "noise_schedule": ("噪声调度（通常保持默认）", "NovelAI、砂糖云使用；不确定时保持默认。"),
    "novelai_api_mode": ("NovelAI 接口类型", "官方直连选官方接口；YesNovelAI 等 /v1/nai/generate-image 服务选 JSON 接口。"),
    "novelai_mode": ("NovelAI 图片类型", "选择动漫、兽人或背景，须与服务商模型类型一致。"),
    "novelai_request_timeout_seconds": ("NovelAI 请求最多等待多久（秒）", "较慢的 JSON 服务可适当调大，最大 310 秒。"),
    "endpoint_path": ("百炼接口路径", "按百炼模型文档填写，一般保持默认。"),
    "dashscope_async": ("百炼异步生成", "万相 2.5、可灵会自动启用，其他模型按需设置。"),
    "prompt_extend": ("让百炼自动扩写描述", "建议开启，百炼会进一步整理画图描述。"),
}

# 现有页面直接显示枚举值；将中文含义写入说明，保留原有配置值。
CHOICE_MEANINGS = {
    "format": {"openai": "OpenAI 兼容 / 中转服务", "openai-chat": "OpenAI 对话接口生图", "gemini": "Google Gemini", "doubao": "豆包 / 火山方舟", "modelscope": "魔搭", "shatangyun": "砂糖云", "novelai": "NovelAI", "mengyuai": "梦羽", "comfyui": "ComfyUI 工作流", "dashscope": "阿里百炼"},
    "selfie_style": {"auto": "自动选择", "standard": "前置自拍", "mirror": "对镜自拍", "photo": "他人拍摄"},
    "period": {"daily": "每天", "weekly": "每周", "monthly": "每月", "once": "一次性总额"},
    "activity_type": {"sleeping": "睡眠", "waking_up": "起床", "eating": "吃饭", "working": "工作", "studying": "学习", "exercising": "运动", "relaxing": "休息", "socializing": "社交", "commuting": "通勤", "hobby": "兴趣活动", "self_care": "自我护理", "cooking": "做饭", "shopping": "购物", "gaming": "游戏", "bathing": "洗澡", "other": "其他"},
    "prompt_syntax": {"auto": "自动选择", "natural": "自然语言", "tags": "标签"},
    "novelai_api_mode": {"official": "官方接口", "json": "JSON 接口 / YesNovelAI"},
    "novelai_mode": {"anime": "动漫", "furry": "兽人", "background": "背景"},
    "target_type": {"group": "群聊共享", "user": "私聊用户"},
}


def _choice_help(name: str) -> str:
    return "；".join(f"{value}：{label}" for value, label in CHOICE_MEANINGS.get(name, {}).items())


def _decorate_items(field: Dict[str, Any], model: Any, texts: Dict[str, Any]) -> None:
    """列表只支持标签、占位文字和基础控件，不依赖宿主的额外渲染功能。"""
    for name, item in field["item_fields"].items():
        info = model.model_fields[name]
        extra = info.json_schema_extra or {}
        for key in ("placeholder", "min", "max", "step"):
            if key in extra:
                item[key] = extra[key]
        label, hint = texts.get(name, (item["label"], info.description or ""))
        item["label"] = label
        item["placeholder"] = extra.get("placeholder") or hint


def build_config_ui(schema: Dict[str, Any]) -> Dict[str, Any]:
    """补全列表展示信息，并将分区按真实配置路径拆到不同标签页。"""
    sections = schema["sections"]
    for section_name, section in sections.items():
        for name, field in section["fields"].items():
            if text := FIELD_TEXT.get(f"{section_name}.{name}"):
                field["label"], field["hint"] = text
            elif not field.get("hint"):
                field["hint"] = field["description"]
            if field.get("input_type") in {"textarea", "password"}:
                field["ui_type"] = field["input_type"]
            choice_key = "selfie_style" if name == "default_style" else "activity_type" if name == "disabled_activity_types" else name
            if help_text := _choice_help(choice_key):
                field["hint"] += f" 选项含义：{help_text}。"

    model_list = sections["models"]["fields"]["items"]
    _decorate_items(model_list, ModelConfig, MODEL_TEXT)
    model_fields = model_list["item_fields"]
    common_fields = [
        "format", "base_url", "api_key", "model", "name", "id", "support_img2img",
        "default_size", "custom_prompt_add", "negative_prompt_add",
    ]
    model_fields["id"].update(default="", placeholder="留空保存后自动编号，也可填写 model2")
    model_fields["format"]["label"] = "绘图服务类型（openai 为 OpenAI 兼容）"
    model_fields["api_key"].update(label="API密钥（复制服务商密钥即可）", placeholder="直接粘贴密钥，OpenAI 兼容会自动补 Bearer")
    model_fields["seed"]["label"] = "随机种子（-1 为随机）"
    model_fields["guidance_scale"]["label"] = "描述对画面的影响强度（通常保持默认）"
    model_fields["num_inference_steps"]["label"] = "生成步数（通常保持默认）"
    model_fields["prompt_syntax"]["label"] = "提示词格式（auto 自动 / natural 自然语言 / tags 标签）"
    model_fields["novelai_api_mode"]["label"] = "NovelAI 接口类型（official 官方 / json 中转）"
    model_fields["novelai_mode"]["label"] = "NovelAI 图片类型（anime 动漫 / furry 兽人 / background 背景）"
    model_fields["cfg"]["label"] = "NovelAI / 砂糖云：CFG Rescale（通常保持默认）"
    model_fields["sampler"]["label"] = "NovelAI / 砂糖云：采样方式（通常保持默认）"
    model_fields["noise_schedule"]["label"] = "NovelAI / 砂糖云：噪声调度（通常保持默认）"
    model_fields["artist"]["label"] = "NovelAI / 砂糖云：艺术家风格标签"
    model_fields["novelai_quality_tags"]["label"] = "NovelAI 自动添加质量标签"
    model_fields["novelai_smea"]["label"] = "NovelAI SMEA（仅 V3 文生图）"
    model_fields["novelai_smea_dynamic"]["label"] = "NovelAI 动态 SMEA（需同时开启 SMEA）"
    model_fields["novelai_img2img_strength"]["label"] = "NovelAI 普通图生图的改动强度（0–1）"
    model_fields["novelai_img2img_noise"]["label"] = "NovelAI 图生图附加噪声（0–1）"
    # 列表编辑器按字典顺序显示；连接必填项和常用设置靠前，
    # 每个模型只保留一张完整表单，避免到另一处寻找参数。
    model_list["item_fields"] = {
        name: model_fields[name] for name in common_fields + [name for name in model_fields if name not in common_fields]
    }
    model_list["hint"] = (
        "先填服务类型、地址、密钥和服务商模型名称，其余通常保持默认。编号留空保存后自动分配。"
        f"服务类型含义：{_choice_help('format')}。"
        "正面增强词会追加到每张图，负面提示词填写要避免的内容。"
        "使用 NovelAI 中转时，将下方 NovelAI 接口类型设为 json。"
    )

    _decorate_items(sections["selfie"]["fields"]["activity_rules"], ActivityRule, {})
    _decorate_items(sections["quota"]["fields"]["rules"], QuotaRule, {
        "platform": ("平台（通常保留 qq）", "填写适配器的平台标识。"),
        "target_type": ("目标类型（group 群聊 / user 私聊）", "group 为整个群共享，user 仅用于私聊。"),
        "target_id": ("群或用户 ID", "填写实际群 ID 或私聊用户 ID。"),
        "limit": ("每个周期最多生成几张", "0 表示禁止该群或用户生图。"),
        "period": ("次数重置周期（留空沿用全局）", "daily 每天 / weekly 每周 / monthly 每月 / once 总额"),
    })
    sections["quota"]["fields"]["rules"]["item_fields"]["period"].update(type="string", default="")
    model_selectors = {
        "basic": {"default_txt2img_model": "使用 model1", "default_img2img_model": "沿用默认文生图模型", "pic_command_model": None},
        "selfie": {"llm_selfie_model": "按参考图自动选择", "selfie_model": "沿用聊天自拍模型"},
        "group_image": {"model_id": "自动选择"},
    }
    for section_name, selectors in model_selectors.items():
        for name, empty_label in selectors.items():
            sections[section_name]["fields"][name].update(
                ui_type="text", placeholder=f"填写配置编号；留空{empty_label}" if empty_label else "填写配置编号，例如 model1",
            )

    def split(source: str, key: str, title: str, names: List[str], *, collapsed: bool = False, description: str = "") -> None:
        section = sections[source]
        sections[key] = {
            **section, "name": source, "title": title, "description": description,
            "collapsed": collapsed, "fields": {name: section["fields"].pop(name) for name in names},
        }

    split("basic", "basic_advanced", "生图等待、缓存与排查", ["max_retries", "enable_verbose_debug", "cache_enabled", "cache_max_size", "auto_recall_enabled", "llm_task_name", "llm_timeout_seconds", "generation_timeout_seconds", "proactive_min_energy"], collapsed=True, description="通常保持默认；生成较慢或需要排查问题时再调整。")
    split("basic", "model_defaults", "默认使用哪个模型", ["default_txt2img_model", "default_img2img_model", "pic_command_model"], description="填写上方模型的配置编号；只用一个模型时通常保持默认。")
    split("selfie", "selfie_chat", "聊天中的自拍", ["schedule_enabled", "llm_selfie_model"], description="用户请求自拍时的模型与日程设置。")
    split("selfie", "selfie_auto", "主动分享日程自拍", ["auto_enabled", "selfie_model", "group_enabled", "qzone_enabled", "check_interval_minutes", "interval_minutes", "probability", "quiet_hours_start", "quiet_hours_end", "caption_enabled", "caption_text"], collapsed=True, description="需要 MaiTrace；开启后按日程选择分享渠道。")
    split("selfie", "selfie_advanced", "自拍活动规则", ["cycle_timeout_seconds", "disabled_activity_types", "activity_rules"], collapsed=True, description="所有自拍入口共用的活动限制和服装规则。")
    split("group_image", "group_image_shared", "允许群与免打扰", ["platform", "group_ids", "cooldown_minutes", "quiet_hours_start", "quiet_hours_end"], description="日程自拍发群和群话题图片共用；空名单不发群。")
    split("group_image", "group_image_advanced", "群话题判断设置", ["context_messages", "context_minutes", "disabled_activity_types", "cycle_timeout_seconds"], collapsed=True, description="聊天参考范围、活动限制和等待时间，通常保持默认。")
    sections["plugin"].update(title="开始使用", description="先到「模型配置」填写绘图服务，再开启插件并保存；用 /dr 画一只猫 验证。")
    sections["plugin"]["fields"]["name"]["hidden"] = True
    sections["plugin"]["fields"]["config_version"]["hidden"] = True
    sections["models"].update(title="模型列表", description="此处始终显示完整表单。启用插件并保存后，可打开侧栏「绘卷控制台」按格式即时切换参数。", collapsed=False)
    sections["basic"].update(title="聊天与命令画图", description="选择默认模型和使用方式；不确定的选项可以保持默认。")
    sections["selfie"].update(title="角色形象与拍照方式", description="先填角色外貌；有参考图时再填写图片路径。聊天自拍与主动分享共用。")
    sections["group_image"].update(title="根据群话题主动发图", description="与日程自拍分别开启；共用允许群名单和群免打扰设置。", collapsed=True)
    sections["quota"].update(title="聊天生图次数限制", description="群内共享次数，私聊按用户计算；默认关闭。")
    sections["styles"].update(title="常用画风", description="设置后可使用 /dr 水彩 等命令。")
    sections["comfyui"].update(title="ComfyUI 等待设置", description="聊天与页面绘图的外层时限为 660 秒；日程自拍和群图还受各自周期时限限制。", collapsed=True)
    sections["proxy"].update(title="网络代理", description="服务需要代理访问时再开启。", collapsed=True)
    schema["layout"] = {"type": "tabs", "tabs": [
        {"id": "start", "title": "开始使用", "sections": ["plugin", "basic"]},
        {"id": "models", "title": "模型配置", "sections": ["models", "model_defaults"]},
        {"id": "selfie", "title": "自拍与分享", "sections": ["selfie", "selfie_chat", "group_image_shared", "selfie_auto", "group_image"]},
        {"id": "styles", "title": "风格预设", "sections": ["styles"]},
        {"id": "quota", "title": "次数限制", "sections": ["quota"]},
        {"id": "advanced", "title": "高级设置", "sections": ["basic_advanced", "selfie_advanced", "group_image_advanced", "comfyui", "proxy"]},
    ]}
    return schema
