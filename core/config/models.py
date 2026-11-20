"""mais_art_journal 配置模型（SDK 2.x 强类型）

v4.2.0 起 section 重新组织：
- BasicSection：合并 generation + components + cache + auto_recall + prompt_optimizer + 命令前缀
- SelfieSection：合并 selfie 模式 + 自动自拍
- ProxySection：保留独立
- StylesSection / ModelsSection：保留独立（含 items 列表）
"""

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator
from typing import List, Literal

from .quota import QuotaSection
from ..selfie.activity_rules import default_activity_rules
from ..selfie.schedule_provider import ActivityType

# ==================== 插件元信息 ====================

class PluginSection(PluginConfigBase):
    """插件启用配置"""

    __ui_label__ = "插件启用配置"
    __ui_icon__ = "info"
    __ui_order__ = 1

    name: str = Field(
        default="麦麦绘卷",
        description="麦麦绘卷（Claude MAInet）— 智能多模型图片生成插件，支持文生图/图生图自动识别",
        json_schema_extra={"label": "插件名称", "disabled": True, "order": 1},
    )
    config_version: str = Field(
        default="4.6.9",
        description="插件配置版本，由插件自动维护，升级时用于备份和迁移已有配置",
        json_schema_extra={"label": "配置版本", "disabled": True, "order": 2},
    )
    enabled: bool = Field(
        default=False,
        description="宿主加载绘卷的总开关；关闭后停止插件组件和后台任务。单个聊天流的生图开关通过 /dr on|off 管理",
        json_schema_extra={"label": "启用插件", "order": 3},
    )


# ==================== 基础配置（合并 6 块） ====================

class BasicSection(PluginConfigBase):
    """基础配置

    合并了图片生成默认配置、组件启用配置、结果缓存配置、自动撤回配置、提示词优化器
    以及命令前缀自定义。
    """

    __ui_label__ = "基础配置"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    # ---- 命令前缀 ----
    command_prefix: str = Field(
        default="/dr",
        description="命令前缀。所有子命令（list/config/set/styles/...）都以此前缀触发。保存后需重新加载插件以更新宿主命令匹配规则",
        json_schema_extra={
            "label": "命令前缀",
            "hint": "默认 /dr，可改成 /draw、/pic 等。必须以 / 开头",
            "placeholder": "/dr",
            "order": 1,
        },
    )

    # ---- 图片生成默认 ----
    default_txt2img_model: str = Field(
        default="",
        description="默认文生图模型ID。未提供参考图的普通生图，以及未指定独立模型的无参考图自拍使用。"
        "留空时使用内置 model1",
        json_schema_extra={
            "label": "默认文生图模型",
            "hint": "对应模型管理中的模型ID（如model1、model2）",
            "placeholder": "model1",
            "order": 10,
        },
    )
    default_img2img_model: str = Field(
        default="",
        description="默认图生图模型ID。图生图（用户在消息中附图或引用图片）时使用，"
        "自拍在有独立自拍模型时优先用自拍模型。留空则回退到「默认文生图模型」",
        json_schema_extra={
            "label": "默认图生图模型",
            "hint": "留空=与文生图模型相同；图生图/带参考图自拍时使用",
            "placeholder": "model2",
            "order": 11,
        },
    )

    # ---- 组件启用 ----
    enable_unified_generation: bool = Field(
        default=True,
        description="是否启用聊天智能生图 Tool；保存后重新加载插件以更新工具入口。关闭后不影响后台主动图片和对外生图 API",
        json_schema_extra={"label": "智能生图", "order": 20},
    )
    enable_pic_command: bool = Field(
        default=True,
        description="是否启用 /dr 风格化与自然语言生图；关闭后仍可使用管理、查询和额度命令",
        json_schema_extra={"label": "图片生成命令", "order": 21},
    )
    max_draws_per_request: int = Field(
        default=1, ge=0, le=20,
        description="同一条最新用户消息允许智能生图 Tool 调用的次数，默认 1，0 不限制次数。"
        "触发消息必须经过宿主核验；记录在重载后保留。进入生图客户端后的失败也占次数。"
        "不限制 /dr 命令、主动图片或外部 API，不等同于宿主回复轮次。",
        json_schema_extra={"label": "每次用户请求最多生图几次", "order": 22},
    )
    pic_command_model: str = Field(
        default="model1",
        description="/dr 自然语言与风格生图的默认模型 ID；/dr set 设置当前聊天流的覆盖值，优先于本字段",
        json_schema_extra={"label": "命令默认模型", "placeholder": "model1", "order": 24},
    )
    admin_users: List[str] = Field(
        default_factory=list,
        description="可管理全局配置和额度的实际用户 ID 列表；官方 QQ 未绑定时填 OpenID，绑定后填统一用户 ID，使用字符串",
        json_schema_extra={
            "label": "管理员列表",
            "hint": "字符串形式的用户ID，如 [\"12345\", \"67890\"]",
            "item_type": "string",
            "placeholder": "[\"用户ID1\", \"用户ID2\"]",
            "order": 25,
        },
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="图片 API 首次请求之外的重试次数，0 不重试；受生图总超时限制。不可重试错误、ComfyUI 排队和 NovelAI 已接收后的异常不重发",
        json_schema_extra={"label": "重试次数", "order": 26},
    )
    enable_debug_info: bool = Field(
        default=False,
        description="是否在聊天中显示调试信息（生成进度、模型名等）",
        json_schema_extra={"label": "调试信息", "order": 27},
    )
    enable_verbose_debug: bool = Field(
        default=False,
        description="为 OpenAI、OpenAI-Chat、DashScope 客户端打印详细请求/响应日志；其他格式仍遵守常规日志级别",
        json_schema_extra={"label": "详细调试", "order": 28},
    )

    # ---- 结果缓存 ----
    cache_enabled: bool = Field(
        default=True,
        description="允许聊天生图复用相同提示词、模型配置、尺寸、强度和源图的内存缓存；对外 API 还需 use_cache=true，主动任务始终不用缓存",
        json_schema_extra={"label": "启用缓存", "order": 40},
    )
    cache_max_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="文生图和图生图各自的内存缓存条数上限，超出时移除最早写入的条目；重启后清空",
        json_schema_extra={
            "label": "最大缓存数",
            "depends_on": "basic.cache_enabled",
            "depends_value": True,
            "order": 41,
        },
    )

    # ---- 自动撤回 ----
    auto_recall_enabled: bool = Field(
        default=False,
        description="聊天生图自动撤回总开关，还需模型 auto_recall_delay>0；SnowLuma/NapCat 调用公开 API，QQ 官方由麦麦调用已有撤回工具，受模型处理和 2 分钟时限影响；主动图和对外 API 不自动撤回",
        json_schema_extra={"label": "启用撤回", "order": 50},
    )

    # ---- 提示词优化器 ----
    prompt_optimizer_enabled: bool = Field(
        default=True,
        description="普通图和自拍是否用辅助 LLM 优化提示词/场景；按模型 prompt_syntax 输出英文标签或自然语言，失败中止生成。关闭后主动发布判断、尺寸选择和可选配文仍会调用 LLM",
        json_schema_extra={"label": "启用优化器", "order": 60},
    )
    llm_task_name: str = Field(
        default="utils",
        description=(
            "辅助 LLM 任务名（对应 MaiBot model_task_config 下的字段名）。"
            "提示词优化、尺寸选择、自拍场景、主动发布判断和配文共用此模型任务；关闭优化器后仍会使用。"
            "常用选项：utils（默认，组件辅助）/ replyer（回复模型）/ planner（规划）/ memory（记忆）/ learner（学习）"
        ),
        json_schema_extra={
            "label": "辅助 LLM 任务",
            "hint": "utils（默认）/ replyer / planner / memory / learner 等，需在主程序 model_task_config 中存在",
            "placeholder": "utils",
            "order": 61,
        },
    )

    llm_timeout_seconds: int = Field(
        default=45, ge=5, le=120,
        description="辅助 LLM 单次调用超时（秒），场景、尺寸、提示词、主动发布判断和配文共用；还受当前生图或主动任务总时限限制",
        json_schema_extra={"label": "辅助 LLM 超时", "order": 62},
    )
    generation_timeout_seconds: int = Field(
        default=170, ge=10, le=600,
        description="普通后端完整生图总超时（秒），含 LLM、图片请求和重试；YesNovelAI 建议 420 秒；ComfyUI 内部至少预留 wait_seconds+60 秒，外层 API/任务时限仍生效",
        json_schema_extra={"label": "生图总超时", "order": 63},
    )
    proactive_min_energy: float = Field(
        default=30, ge=0, le=100,
        description="所有主动发图的最低精力（MaiTrace 生活状态 0-100）；无生活状态时仍必须有有效日程",
        json_schema_extra={"label": "主动发图最低精力", "order": 64},
    )

    @field_validator("command_prefix")
    @classmethod
    def validate_command_prefix(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/") or len(value) < 2 or any(character.isspace() for character in value):
            raise ValueError("命令前缀须以 / 开头、包含命令名称且不能含空白")
        return value


# ==================== 自拍配置（合并自拍模式 + 自动自拍） ====================

class ActivityRule(PluginConfigBase):
    """活动对应的服装与自动构图规则。"""

    activity_type: ActivityType = Field(
        default=ActivityType.OTHER, description="活动类型", json_schema_extra={"label": "活动类型"},
    )
    selfie_style: Literal["standard", "mirror", "photo"] = Field(
        default="standard", description="auto 的优先构图，结合地点、动作和近期照片选择；手动指定构图时遵守指定值", json_schema_extra={"label": "优先构图"},
    )
    outfit: str = Field(
        default="casual everyday clothes", pattern=r"\S", description="活动的服装要求，可用中文或英文",
        json_schema_extra={"label": "活动服装"},
    )


def _default_activity_rules() -> List[ActivityRule]:
    return [ActivityRule(**rule) for rule in default_activity_rules()]


class SelfieSection(PluginConfigBase):
    """自拍配置

    形象、参考图、负面词与活动规则供聊天自拍、自动自拍、群角色照片和外部自拍 API 共用。
    auto_enabled 及其发布设置只控制定时自动自拍。
    """

    __ui_label__ = "自拍配置"
    __ui_icon__ = "camera"
    __ui_order__ = 3

    # ---- 自动自拍与所有自拍入口共用的角色配置 ----
    enabled: bool = Field(
        default=True,
        description="所有自拍入口的总开关，控制聊天自拍、自动自拍、群角色照片与 MaiTrace 自拍；定时发布还需 auto_enabled=true",
        json_schema_extra={"label": "启用自拍", "group": "shared", "order": 1},
    )
    reference_image_path: str = Field(
        default="",
        description="自动自拍及其他自拍入口共用的角色参考图。相对插件目录或绝对路径；留空用文生图，配置后图生图，模型不支持时回退文生图。文件缺失/为空会报错，外部 API 显式源图优先",
        json_schema_extra={
            "label": "自拍参考图（含自动自拍）",
            "group": "shared",
            "placeholder": "images/reference.png",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 2,
        },
    )
    prompt_prefix: str = Field(
        default="",
        description="自动自拍及其他自拍入口共用的固定形象，填写脸型、发型、发色、瞳色等稳定特征，中文或英文均可。与参考图一起保持身份；服装、动作和地点由活动决定，MaiTrace 也读取本字段",
        json_schema_extra={
            "label": "形象配置",
            "group": "shared",
            "input_type": "textarea",
            "rows": 2,
            "placeholder": "blue hair, red eyes, 1girl",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 3,
        },
    )
    negative_prompt: str = Field(
        default="",
        description="所有自拍入口共用的额外负面词，与模型负面词和外部负面词合并；自动补充手部/构图约束。实际独立负面参数支持取决于后端",
        json_schema_extra={
            "label": "负面提示词",
            "group": "shared",
            "input_type": "textarea",
            "rows": 3,
            "placeholder": "lowres, bad anatomy, bad hands, extra fingers",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 4,
        },
    )
    schedule_enabled: bool = Field(
        default=True,
        description="只控制聊天自拍是否查询 MaiTrace 日程/生活状态，可按聊天流用 /dr selfie on|off 覆盖；自动自拍和群主动图始终检查日程，外部 API 使用调用方传入的活动",
        json_schema_extra={
            "label": "聊天自拍日程增强",
            "group": "chat",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 5,
        },
    )
    default_style: Literal["auto", "standard", "mirror", "photo"] = Field(
        default="auto",
        description="聊天自拍和定时自动自拍的默认构图：auto 按活动、standard 前置、mirror 对镜、photo 外部镜头。聊天流设置或调用方显式构图优先；群话题角色照片由发布判断选择构图",
        json_schema_extra={
            "label": "默认自拍构图",
            "group": "shared",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 6,
        },
    )

    # ---- 自动自拍 ----
    auto_enabled: bool = Field(
        default=False,
        description="启用定时自动自拍，使用上方共用形象与参考图。需插件和自拍总开关开启、MaiTrace 有效日程；按生活状态和群话题决定发空间、一个允许群或跳过",
        json_schema_extra={"label": "启用自动自拍", "order": 20},
    )
    interval_minutes: int = Field(
        default=120,
        ge=10,
        le=1440,
        description="两次自动自拍尝试的最短间隔（分钟），空间与群共享；失败或投递未知也保留冷却",
        json_schema_extra={
            "label": "自动自拍冷却",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 21,
        },
    )
    check_interval_minutes: int = Field(
        default=20, ge=1, le=1440,
        description="日程与发布渠道的判断间隔（分钟）；判断跳过不会占用自拍发布冷却",
        json_schema_extra={"label": "判断间隔", "order": 21.1},
    )
    qzone_enabled: bool = Field(
        default=True, description="允许自动自拍选择 QQ 空间；由 MaiTrace 通过 SnowLuma 原生接口或 Cookie HTTP 直发已有图片，需在 MaiTrace 配好空间发布",
        json_schema_extra={"label": "允许发空间", "order": 21.2},
    )
    group_enabled: bool = Field(
        default=True,
        description="允许自动自拍选择群聊。候选沿用 group_image.platform/group_ids、群静默和群冷却；无需开启群话题随机图",
        json_schema_extra={"label": "允许发群", "order": 21.3},
    )
    probability: float = Field(
        default=1.0, ge=0, le=1,
        description="定时自动自拍判断适合分享后的随机通过概率，1 全部通过、0 暂停；不影响聊天自拍、MaiTrace 调用或群话题随机图",
        json_schema_extra={"label": "发布概率", "order": 21.4},
    )
    selfie_model: str = Field(
        default="",
        description="定时自动自拍的专用模型 ID；留空先沿用 llm_selfie_model，再按共用参考图选择默认图生图/文生图模型。不控制群话题随机图和 MaiTrace 指定模型",
        json_schema_extra={
            "label": "自动自拍模型",
            "placeholder": "model2",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 22,
        },
    )
    llm_selfie_model: str = Field(
        default="",
        description="聊天自拍模型 ID，也是自动自拍和群角色照片未指定模型时的备选；聊天流 /dr action_model 和调用方显式模型优先，留空按参考图选择默认模型",
        json_schema_extra={
            "label": "聊天自拍模型（自动自拍备选）",
            "group": "chat",
            "hint": "留空时，有参考图使用默认图生图模型，无参考图使用默认文生图模型；配置参考图时请选支持图生图的模型",
            "placeholder": "model2",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 8,
        },
    )
    quiet_hours_start: str = Field(
        default="00:00",
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
        description="定时自动自拍静默开始（本机时间 HH:MM），支持跨午夜；与结束相同表示不设静默，不影响聊天或 MaiTrace 自拍",
        json_schema_extra={
            "label": "安静开始",
            "placeholder": "00:00",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 23,
        },
    )
    quiet_hours_end: str = Field(
        default="07:00",
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
        description="定时自动自拍静默结束（本机时间 HH:MM）；发群还要满足 group_image 的群静默时段",
        json_schema_extra={
            "label": "安静结束",
            "placeholder": "07:00",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 24,
        },
    )
    caption_enabled: bool = Field(
        default=True,
        description="自动自拍及群话题中的角色照片是否用辅助 LLM 生成配文；关闭改用 caption_text。聊天自拍和 MaiTrace 自行配文不受此项控制",
        json_schema_extra={
            "label": "生成配文",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 25,
        },
    )

    caption_text: str = Field(
        default="记录一下此刻。", min_length=1, pattern=r"\S",
        description="caption_enabled=false 时用于自动自拍和群角色照片的固定正文，不能全为空白；空间和群共用",
        json_schema_extra={"label": "固定配文", "order": 26},
    )
    cycle_timeout_seconds: int = Field(
        default=420, ge=30, le=600,
        description="单次自动自拍取日程、生成、配文与发布的总超时（秒）",
        json_schema_extra={"label": "自动自拍周期超时", "order": 28},
    )
    disabled_activity_types: List[ActivityType] = Field(
        default_factory=lambda: [ActivityType.SLEEPING, ActivityType.BATHING],
        description="这些活动禁止定时自动自拍和群话题角色照片；普通群图由 group_image.disabled_activity_types 控制。睡眠、洗澡、低精力和专注活动另有硬限制",
        json_schema_extra={"label": "禁止自动自拍的活动", "order": 27},
    )
    activity_rules: List[ActivityRule] = Field(
        default_factory=_default_activity_rules,
        description="所有自拍入口共用的活动服装和 auto 优先构图，每种活动最多一条。调用方/MaiTrace 的明确服装优先；优化器关闭仍使用这些规则",
        json_schema_extra={"label": "共用活动服装与构图", "group": "shared", "order": 10},
    )

    @model_validator(mode="after")
    def unique_activity_rules(self) -> "SelfieSection":
        types = [rule.activity_type for rule in self.activity_rules]
        if len(types) != len(set(types)):
            raise ValueError("自拍活动规则的 activity_type 不能重复")
        return self


class GroupImageSection(PluginConfigBase):
    """基于最近群聊与当前活动主动发送图片，默认关闭。"""

    __ui_label__ = "群聊主动图片"
    __ui_icon__ = "image"
    __ui_order__ = 5

    enabled: bool = Field(default=False, description="启用按群话题主动生成普通图或角色照片；不控制定时自动自拍发群", json_schema_extra={"label": "启用群话题随机图", "order": 1})
    platform: str = Field(default="qq", pattern=r"^\S+$", description="允许群的 SDK 平台标识，与自动自拍发群共用", json_schema_extra={"label": "平台", "order": 2})
    group_ids: List[str] = Field(
        default_factory=list, description="群话题随机图和自动自拍共用的实际群 ID 列表；官方 QQ 按绑定情况填 OpenID/统一群 ID。空名单不发群，需宿主已存在真实聊天流", json_schema_extra={"label": "允许群 ID", "order": 3},
    )
    interval_minutes: int = Field(
        default=5, ge=1, le=1440, description="群话题任务轮询及同群判断的最短间隔（分钟）；自动自拍另用 selfie.check_interval_minutes", json_schema_extra={"label": "群话题判断间隔", "order": 4},
    )
    cooldown_minutes: int = Field(
        default=60, ge=1, le=10080, description="每群实际生图尝试的共享冷却（分钟），随机图与日程自拍均遵守；判断不发图不会占用，重启后保留", json_schema_extra={"label": "每群冷却"},
    )
    probability: float = Field(
        default=0.15, ge=0, le=1, description="群话题判断适合分享后的随机通过概率，0 暂停随机图；不影响定时自动自拍发群", json_schema_extra={"label": "群话题发布概率", "order": 6},
    )
    min_messages: int = Field(
        default=5, ge=1, le=100, description="触发群话题判断所需的未消费新消息数；不限制定时自拍候选群，后者只需近期有效消息", json_schema_extra={"label": "最少新消息", "order": 7},
    )
    context_messages: int = Field(
        default=30, ge=1, le=100, description="群话题判断和自动自拍候选群共用的最近消息读取数量，需不小于 min_messages", json_schema_extra={"label": "上下文消息数", "order": 8},
    )
    context_minutes: int = Field(
        default=30, ge=1, le=1440, description="两类主动任务只使用最近这些分钟的非机器人有效消息", json_schema_extra={"label": "上下文时间范围", "order": 9},
    )
    quiet_hours_start: str = Field(
        default="00:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$", description="群话题图片和定时自拍发群共用的静默开始（本机时间 HH:MM）；支持跨午夜，与结束相同表示不设静默",
        json_schema_extra={"label": "群静默开始", "order": 10},
    )
    quiet_hours_end: str = Field(
        default="07:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$", description="群话题图片和定时自拍发群共用的静默结束（本机时间 HH:MM）；不限制空间发布",
        json_schema_extra={"label": "群静默结束", "order": 11},
    )
    model_id: str = Field(default="", description="群话题图片专用模型，普通图和角色照片均优先用它；留空时普通图用默认文生图，角色照片先沿用 llm_selfie_model，再按参考图选默认模型", json_schema_extra={"label": "群话题生图模型", "order": 12})
    disabled_activity_types: List[ActivityType] = Field(
        default_factory=lambda: [ActivityType.SLEEPING, ActivityType.BATHING],
        description="这些活动期间不主动发群图；所有主动图都要求有效日程，睡眠、低精力和专注活动另有统一限制",
        json_schema_extra={"label": "禁止主动发图的活动"},
    )
    cycle_timeout_seconds: int = Field(
        default=240, ge=30, le=600, description="单群判断、生成、发送的总超时（秒）", json_schema_extra={"label": "单群周期超时"},
    )

    @model_validator(mode="after")
    def validate_context_size(self) -> "GroupImageSection":
        if self.min_messages > self.context_messages:
            raise ValueError("group_image.context_messages 不能小于 min_messages")
        return self


# ==================== ComfyUI ====================

class ComfyUISection(PluginConfigBase):
    """ComfyUI 工作流提交与结果轮询设置。"""

    __ui_label__ = "ComfyUI"
    __ui_icon__ = "image"
    __ui_order__ = 12

    client_id: str = Field(
        default="maibot-hilda",
        min_length=1,
        pattern=r"^\S+$",
        description="ComfyUI 队列归属标识，同一实例使用固定值，多机器人连接同一服务时应各自设置不同值",
        json_schema_extra={"label": "客户端标识", "order": 1},
    )
    wait_seconds: int = Field(
        default=600,
        ge=30,
        le=3600,
        description="ComfyUI /history 轮询上限（秒）。内部总时限至少 wait_seconds+60，仍受外层 Tool/命令/API 的 660 秒和主动任务周期超时限制；proxy.timeout 不控制轮询",
        json_schema_extra={"label": "出图等待上限（秒）", "order": 2},
    )


# ==================== 代理（独立） ====================

class ProxySection(PluginConfigBase):
    """代理设置"""

    __ui_label__ = "代理设置"
    __ui_icon__ = "globe"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="为绘卷自己的图片 API 请求和结果下载启用代理；MaiBot 辅助 LLM、适配器消息和 MaiTrace 空间请求由各自配置控制",
        json_schema_extra={"label": "启用代理", "order": 1},
    )
    url: str = Field(
        default="http://127.0.0.1:7890",
        description="代理服务器地址",
        json_schema_extra={
            "label": "代理地址",
            "hint": "使用 HTTP 或 HTTPS 代理地址；OpenAI/OpenAI-Chat 暂不支持 SOCKS5 代理",
            "placeholder": "http://127.0.0.1:7890",
            "depends_on": "proxy.enabled",
            "depends_value": True,
            "order": 2,
        },
    )
    timeout: int = Field(
        default=60,
        ge=10,
        le=300,
        description="启用代理时多数图片客户端的单次 HTTP 超时（秒），含响应等待；不是生成总时限。ComfyUI 使用自身请求/轮询超时，原生 NovelAI 使用 novelai_request_timeout_seconds",
        json_schema_extra={
            "label": "代理请求超时",
            "depends_on": "proxy.enabled",
            "depends_value": True,
            "order": 3,
        },
    )


# ==================== 风格管理 ====================

class StylePreset(PluginConfigBase):
    """单个风格预设"""

    __ui_label__ = "风格预设"

    name: str = Field(
        default="cartoon",
        description="风格英文名（命令引用，不能重复）",
        json_schema_extra={
            "label": "风格名",
            "placeholder": "cartoon",
            "group": "info",
            "order": 1,
        },
    )
    aliases: str = Field(
        default="",
        description="风格中文别名，逗号分隔。例如 '卡通,动漫'",
        json_schema_extra={
            "label": "中文别名",
            "hint": "逗号分隔，可留空",
            "placeholder": "卡通,动漫",
            "group": "info",
            "order": 2,
        },
    )
    prompt: str = Field(
        default="cartoon style, anime style, colorful, vibrant colors, clean lines",
        description="/dr <风格> 使用的风格描述，支持中文或英文；开启优化器时按模型语法处理，再附加模型增强词",
        json_schema_extra={
            "label": "提示词",
            "input_type": "textarea",
            "rows": 3,
            "group": "prompt",
            "order": 3,
        },
    )


# ==================== 模型管理 ====================

class ModelConfig(PluginConfigBase):
    """单个图片生成模型配置"""

    __ui_label__ = "模型配置"

    id: str = Field(
        default="model1",
        description="模型唯一标识（命令引用用，不可重复）",
        json_schema_extra={
            "label": "模型ID",
            "hint": "建议使用 model1 / model2 / model3 等简短标识",
            "placeholder": "model1",
            "group": "connection",
            "order": 0,
        },
    )
    name: str = Field(
        default="我的绘图服务",
        description="模型显示名称",
        json_schema_extra={"label": "模型名称", "group": "connection", "order": 1},
    )
    base_url: str = Field(
        default="https://api-inference.modelscope.cn/v1",
        description="API 服务地址；原生 NAI 的 JSON 模式可填站点根地址、/v1 基址或完整 /v1/nai/generate-image 地址",
        json_schema_extra={
            "label": "API地址",
            "placeholder": "https://api.example.com/v1",
            "group": "connection",
            "order": 2,
        },
    )
    api_key: str = Field(
        default="",
        description="图片服务密钥；openai/openai-chat 的裸 token 自动补 Bearer，已有认证方案（如 Bearer/Token）保留；"
        "其他云后端支持原始 token 或 Bearer token，ComfyUI 可留空；不与 MaiBot 辅助 LLM 密钥共用",
        json_schema_extra={
            "label": "API密钥",
            "input_type": "password",
            "placeholder": "sk-xxx 或 Bearer sk-xxx",
            "group": "connection",
            "order": 3,
        },
    )
    format: Literal[
        "openai", "openai-chat", "gemini", "doubao", "modelscope",
        "shatangyun", "novelai", "mengyuai", "comfyui", "dashscope",
    ] = Field(
        default="openai",
        description="选择实际图片服务协议，影响请求字段、尺寸和图生图支持；仅修改模型名称不会切换协议",
        json_schema_extra={"label": "API格式", "group": "connection", "order": 4},
    )
    model: str = Field(
        default="cancel13/liaocao",
        description="服务端模型名称；梦羽填文生图数字索引，ComfyUI 填 workflow/ 下的工作流文件名或绝对路径，与本地模型 ID 不同",
        json_schema_extra={
            "label": "模型标识",
            "placeholder": "model-name / 0 / workflow.json",
            "group": "connection",
            "order": 5,
        },
    )
    fixed_size_enabled: bool = Field(
        default=False,
        description="开启时使用 default_size（覆盖本次请求尺寸）；关闭时优先本次尺寸，再用辅助 LLM 选择，失败用 default_size。-2K 等写法仍选择比例",
        json_schema_extra={"label": "固定尺寸", "group": "params", "order": 6},
    )
    default_size: str = Field(
        default="1024x1024",
        description="固定尺寸或尺寸选择失败时的备选。扩散/NovelAI/ComfyUI 用像素尺寸，Gemini 等支持比例/分辨率，格式由后端决定",
        json_schema_extra={
            "label": "默认尺寸",
            "placeholder": "1024x1024 / 16:9-2K / 2K",
            "group": "params",
            "order": 7,
        },
    )
    seed: int = Field(
        default=-1,
        ge=-1,
        le=2147483647,
        description="支持该参数的后端使用的随机种子，-1 表示随机或不传；Gemini 忽略，梦羽/魔搭图生图不发送，其他后端按服务能力处理",
        json_schema_extra={"label": "随机种子", "group": "params", "order": 8},
    )
    guidance_scale: float = Field(
        default=2.5,
        ge=-1.0,
        le=20.0,
        description="扩散引导强度。OpenAI/豆包/魔搭/砂糖云/梦羽支持 -1 不发送；ComfyUI 的 -1 使用 7。原生 NovelAI 官方模式 0.1-20，JSON 模式 0-10；Gemini/OpenAI-Chat/DashScope 不使用",
        json_schema_extra={"label": "引导强度", "step": 0.5, "group": "params", "order": 9},
    )
    num_inference_steps: int = Field(
        default=20,
        ge=-1,
        le=150,
        description="扩散推理步数；OpenAI 可填 -1 不发送，其他使用步数的后端须为正数。原生 NovelAI 1-50；Gemini/OpenAI-Chat/豆包/DashScope 不使用",
        json_schema_extra={"label": "推理步数", "group": "params", "order": 10},
    )
    watermark: bool = Field(
        default=True,
        description="豆包、DashScope 及 OpenAI 格式连接火山方舟时发送的水印开关；其他客户端不发送，不能去除服务强制水印",
        json_schema_extra={"label": "水印", "group": "params", "order": 11},
    )
    custom_prompt_add: str = Field(
        default="",
        description="追加到所有该模型图片请求的正面描述/风格词，普通图和自拍共用；直接拼接，建议带逗号或空格，与自拍固定形象配置不同",
        json_schema_extra={
            "label": "正面增强词",
            "input_type": "textarea",
            "rows": 2,
            "group": "prompts",
            "order": 12,
        },
    )
    negative_prompt_add: str = Field(
        default="Pornography,nudity,lowres, bad anatomy, bad hands, text, error",
        description="模型基础负面词，与自拍/调用方负面词合并。Gemini、豆包及梦羽/魔搭图生图不发送独立负面字段；OpenAI-Chat 将其写入文本要求",
        json_schema_extra={
            "label": "负面提示词",
            "input_type": "textarea",
            "rows": 2,
            "group": "prompts",
            "order": 13,
        },
    )
    artist: str = Field(
        default="",
        description="艺术家风格标签（砂糖云 / 原生 NovelAI）",
        json_schema_extra={"label": "艺术家标签", "group": "prompts", "order": 14},
    )
    support_img2img: bool = Field(
        default=True,
        description="声明模型支持参考图；false 时清除源图和强度，使用同一模型文生图。需服务实际支持：砂糖云客户端仅文生图，ComfyUI 工作流需含 ${image}",
        json_schema_extra={"label": "支持图生图", "group": "prompts", "order": 15},
    )
    prompt_syntax: Literal["auto", "natural", "tags"] = Field(
        default="auto",
        description="普通图与自拍共用的提示词语法。auto 对扩散模型使用标签，对 OpenAI/Gemini/豆包/百炼使用自然语言；兼容网关可明确选择",
        json_schema_extra={"label": "提示词语法", "group": "prompts", "order": 17},
    )
    selfie_reference_strength: float = Field(
        default=0.6, ge=0.1, le=1.0,
        description="有参考图的自拍重绘/降噪强度，越大通常改动越大；用于 ComfyUI、原生 NovelAI 和 prompt_syntax=tags 的 OpenAI 扩散网关。显式 strength 优先，其他后端忽略",
        json_schema_extra={"label": "自拍参考图强度", "group": "prompts", "order": 18},
    )
    auto_recall_delay: int = Field(
        default=0,
        ge=0,
        le=120,
        description="自动撤回延时（秒）。大于 0 启用，0 不撤回。需先开启撤回总开关；QQ 官方需小于 120 秒，并为麦麦处理任务预留时间",
        json_schema_extra={"label": "撤回延时", "group": "prompts", "order": 16},
    )
    cfg: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="砂糖云 / 原生 NovelAI：CFG Rescale",
        json_schema_extra={"label": "CFG Rescale", "step": 0.1, "group": "platform", "order": 20},
    )
    sampler: Literal[
        "k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
        "k_dpmpp_2m_sde", "k_dpmpp_2m", "k_dpmpp_sde",
    ] = Field(
        default="k_euler_ancestral",
        description="砂糖云 / 原生 NovelAI：采样器",
        json_schema_extra={"label": "采样器", "group": "platform", "order": 21},
    )
    nocache: int = Field(
        default=0,
        ge=0,
        le=1,
        description="砂糖云服务端缓存开关，0 使用、1 禁用；不控制绘卷 basic.cache_enabled",
        json_schema_extra={"label": "禁用缓存", "group": "platform", "order": 22},
    )
    noise_schedule: Literal[
        "karras", "native", "exponential", "polyexponential",
    ] = Field(
        default="karras",
        description="砂糖云 / 原生 NovelAI：噪声调度",
        json_schema_extra={"label": "噪声调度", "group": "platform", "order": 23},
    )

    # ---- 梦羽 AI 图生图专属 ----
    img2img_model_index: int = Field(
        default=19, ge=0,
        description="仅梦羽图生图使用的数字模型索引，默认 19（Qwen Image Edit）；文生图仍使用 model",
        json_schema_extra={"label": "梦羽图生图模型索引", "group": "mengyuai", "order": 24},
    )
    image_upload_url: str = Field(
        default="",
        description="仅梦羽图生图使用的图片上传服务 URL，上传后取得公网图片地址；留空尝试 data URI，服务可能不支持",
        json_schema_extra={"label": "梦羽图片上传地址", "group": "mengyuai", "order": 24.1},
    )

    # SDK 的列表表单不展开嵌套对象，NAI 选项直接放在每个模型中。
    novelai_api_mode: Literal["official", "json"] = Field(
        default="official",
        description="仅原生 NAI：official 使用官方图片接口；json 使用 /v1/nai/generate-image，返回 JSON/Base64",
        json_schema_extra={"label": "NAI 接口类型", "group": "novelai", "order": 25},
    )
    novelai_mode: Literal["anime", "furry", "background"] = Field(
        default="anime",
        description="NAI 数据集模式；V3 须与模型对应，background 仅 V4.5/V5",
        json_schema_extra={"label": "NAI 数据集模式", "group": "novelai", "order": 26},
    )
    novelai_quality_tags: bool = Field(
        default=True,
        description="原生 NAI：在本地按模型版本追加质量标签",
        json_schema_extra={"label": "NAI 添加质量标签", "group": "novelai", "order": 27},
    )
    novelai_smea: bool = Field(
        default=False,
        description="原生 NAI：SMEA，仅支持 V3 文生图",
        json_schema_extra={"label": "NAI SMEA", "group": "novelai", "order": 28},
    )
    novelai_smea_dynamic: bool = Field(
        default=False,
        description="原生 NAI：动态 SMEA，须同时开启 SMEA",
        json_schema_extra={"label": "NAI 动态 SMEA", "group": "novelai", "order": 29},
    )
    novelai_img2img_strength: float = Field(
        default=0.6, ge=0.0, le=1.0,
        description="原生 NAI 普通图生图强度；自拍使用 selfie_reference_strength，本次显式强度优先",
        json_schema_extra={"label": "NAI 图生图强度", "group": "novelai", "order": 30},
    )
    novelai_img2img_noise: float = Field(
        default=0.1, ge=0.0, le=1.0,
        description="原生 NAI 图生图的附加噪声，文生图不使用",
        json_schema_extra={"label": "NAI 图生图噪声", "group": "novelai", "order": 31},
    )
    novelai_request_timeout_seconds: int = Field(
        default=120, ge=5, le=310,
        description="原生 NAI 单次 HTTP 超时；YesNovelAI 建议 310 秒，同时将基础生图总超时设为 420 秒",
        json_schema_extra={"label": "NAI 请求超时（秒）", "group": "novelai", "order": 32},
    )

    # ---- DashScope（阿里百炼）专属 ----
    endpoint_path: str = Field(
        default="",
        description=(
            "DashScope 端点 path：留空走多模态默认 "
            "(/api/v1/services/aigc/multimodal-generation/generation，千问/万相2.6+/Z-Image)；"
            "万相 2.5 i2i 填 /api/v1/services/aigc/image2image/image-synthesis；"
            "可灵填 /api/v1/services/aigc/image-generation/generation。"
            "其他 format 留空即可。"
        ),
        json_schema_extra={
            "label": "DashScope 端点 path",
            "hint": "仅 format=dashscope 时使用",
            "placeholder": "/api/v1/services/aigc/multimodal-generation/generation",
            "group": "dashscope",
            "order": 30,
        },
    )
    dashscope_async: bool = Field(
        default=False,
        description=(
            "DashScope 是否启用异步（X-DashScope-Async: enable 头）。"
            "万相 2.5 / 可灵端点会自动启用，其他模型按需开启。"
        ),
        json_schema_extra={
            "label": "DashScope 异步模式",
            "group": "dashscope",
            "order": 31,
        },
    )
    prompt_extend: bool = Field(
        default=True,
        description="DashScope 智能扩写。开启后阿里会自动优化你的提示词，建议保持开启。",
        json_schema_extra={
            "label": "DashScope 智能扩写",
            "group": "dashscope",
            "order": 32,
        },
    )

    @model_validator(mode="after")
    def validate_backend_parameters(self) -> "ModelConfig":
        if self.num_inference_steps == 0:
            raise ValueError("num_inference_steps 不能为 0；正数为步数，-1 仅供支持省略的后端")
        if self.num_inference_steps <= 0 and self.format in {"modelscope", "shatangyun", "mengyuai", "comfyui", "novelai"}:
            raise ValueError(f"{self.format} 的 num_inference_steps 须为正数，-1 仅供 OpenAI 网关省略参数")
        if self.format == "novelai":
            minimum, maximum = (0, 10) if self.novelai_api_mode == "json" else (0.1, 20)
            if self.num_inference_steps > 50 or not minimum <= self.guidance_scale <= maximum:
                raise ValueError(f"原生 NovelAI 步数须为 1-50，guidance_scale 须为 {minimum}-{maximum}")
        return self


# ==================== Section 包装：解决 List 字段保存丢失 ====================

def _default_styles() -> List[StylePreset]:
    return [
        StylePreset(
            name="cartoon",
            aliases="卡通,动漫",
            prompt="cartoon style, anime style, colorful, vibrant colors, clean lines",
        ),
        StylePreset(
            name="watercolor",
            aliases="水彩",
            prompt="watercolor painting style, soft colors, artistic",
        ),
    ]


def _default_models() -> List[ModelConfig]:
    return [ModelConfig()]


class StylesSection(PluginConfigBase):
    """风格管理"""

    __ui_label__ = "风格管理"
    __ui_icon__ = "palette"
    __ui_order__ = 10

    items: List[StylePreset] = Field(
        default_factory=_default_styles,
        description="/dr <风格> 的预设列表，每项含唯一英文名、逗号分隔别名和风格描述；可在 WebUI 增删",
        json_schema_extra={"label": "风格列表", "order": 1},
    )

    @model_validator(mode="after")
    def unique_names(self) -> "StylesSection":
        names = [item.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("风格 name 不能重复")
        return self


class ModelsSection(PluginConfigBase):
    """模型管理"""

    __ui_label__ = "模型管理"
    __ui_icon__ = "cpu"
    __ui_order__ = 11

    items: List[ModelConfig] = Field(
        default_factory=_default_models,
        description="多模型列表。每项有独立 id 和 API 配置。可在 WebUI 增删",
        json_schema_extra={"label": "模型列表", "order": 1},
    )

    @model_validator(mode="after")
    def unique_ids(self) -> "ModelsSection":
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("模型 id 不能重复")
        return self


# ==================== 根配置 ====================

class MaisArtConfig(PluginConfigBase):
    """麦麦绘卷根配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    basic: BasicSection = Field(default_factory=BasicSection)
    selfie: SelfieSection = Field(default_factory=SelfieSection)
    group_image: GroupImageSection = Field(default_factory=GroupImageSection)
    comfyui: ComfyUISection = Field(default_factory=ComfyUISection)
    quota: QuotaSection = Field(default_factory=QuotaSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    styles: StylesSection = Field(default_factory=StylesSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
