"""mais_art_journal 配置模型（SDK 2.x 强类型）

v4.2.0 起 section 重新组织：
- BasicSection：合并 generation + components + cache + auto_recall + prompt_optimizer + 命令前缀
- SelfieSection：合并 selfie 模式 + 自动自拍
- ProxySection：保留独立
- StylesSection / ModelsSection：保留独立（含 items 列表）
"""

from typing import List, Literal

from maibot_sdk import Field, PluginConfigBase


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
        default="4.2.0",
        description="插件配置版本号",
        json_schema_extra={"label": "配置版本", "disabled": True, "order": 2},
    )
    enabled: bool = Field(
        default=False,
        description="是否启用插件，开启后可使用画图和风格转换功能",
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
        description="命令前缀。所有子命令（list/config/set/styles/...）都以此前缀触发。修改后下一条命令即生效",
        json_schema_extra={
            "label": "命令前缀",
            "hint": "默认 /dr，可改成 /draw、/pic 等。必须以 / 开头",
            "placeholder": "/dr",
            "order": 1,
        },
    )

    # ---- 图片生成默认 ----
    # 旧 default_model 保留为兼容字段：旧 config.toml 里只有它，解析层会把它的值
    # 作为文生图默认的回退来源，用户改用新字段后它不再参与路由。
    default_model: str = Field(
        default="",
        description="[已废弃] 旧版默认模型，留空即可。值会自动作为「默认文生图模型」的回退",
        json_schema_extra={"label": "默认模型(已废弃)", "order": 9, "hidden": True},
    )
    default_txt2img_model: str = Field(
        default="",
        description="默认文生图模型ID。纯文生图（用户未提供参考图、非自拍）时使用。"
        "留空时依次回退到旧 default_model、内置 model1",
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
        description="是否启用智能图片生成 Action，支持文生图和图生图自动识别",
        json_schema_extra={"label": "智能生图", "order": 20},
    )
    enable_pic_command: bool = Field(
        default=True,
        description="是否启用图片生成命令，支持风格化图生图和自然语言文/图生图",
        json_schema_extra={"label": "图片生成命令", "order": 21},
    )
    enable_pic_config: bool = Field(
        default=True,
        description="是否启用模型配置管理命令（list / config / set 等）",
        json_schema_extra={"label": "配置管理", "order": 22},
    )
    enable_pic_style: bool = Field(
        default=True,
        description="是否启用风格管理命令（styles / style 等）",
        json_schema_extra={"label": "风格管理", "order": 23},
    )
    pic_command_model: str = Field(
        default="model1",
        description="命令组件使用的默认模型ID，可通过 set 命令动态切换",
        json_schema_extra={"label": "命令默认模型", "placeholder": "model1", "order": 24},
    )
    admin_users: List[str] = Field(
        default_factory=list,
        description="有权限使用配置管理命令的管理员用户列表",
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
        description="API调用失败时的重试次数，建议2-5次。设置为0表示不重试",
        json_schema_extra={"label": "重试次数", "order": 26},
    )
    enable_debug_info: bool = Field(
        default=False,
        description="是否在聊天中显示调试信息（生成进度、模型名等）",
        json_schema_extra={"label": "调试信息", "order": 27},
    )
    enable_verbose_debug: bool = Field(
        default=False,
        description="是否打印完整的 API 请求/响应报文到日志",
        json_schema_extra={"label": "详细调试", "order": 28},
    )

    # ---- 结果缓存 ----
    cache_enabled: bool = Field(
        default=True,
        description="是否启用结果缓存，相同参数的请求复用之前的结果",
        json_schema_extra={"label": "启用缓存", "order": 40},
    )
    cache_max_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="最大缓存数量，超出后删除最旧的缓存",
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
        description="是否启用自动撤回功能（总开关）。关闭后所有模型的撤回都不生效",
        json_schema_extra={"label": "启用撤回", "order": 50},
    )

    # ---- 提示词优化器 ----
    prompt_optimizer_enabled: bool = Field(
        default=True,
        description="是否启用提示词优化器。开启后会使用 MaiBot 主 LLM 将用户描述优化为专业英文提示词",
        json_schema_extra={"label": "启用优化器", "order": 60},
    )
    llm_task_name: str = Field(
        default="utils",
        description=(
            "辅助 LLM 任务名（对应 MaiBot model_task_config 下的字段名）。"
            "插件的提示词优化、尺寸选择、自拍场景/动作/配文生成都会调用此任务对应的模型。"
            "常用选项：utils（默认，组件辅助）/ replyer（回复模型）/ planner（规划）/ memory（记忆）/ learner（学习）"
        ),
        json_schema_extra={
            "label": "辅助 LLM 任务",
            "hint": "utils（默认）/ replyer / planner / memory / learner 等，需在主程序 model_task_config 中存在",
            "placeholder": "utils",
            "depends_on": "basic.prompt_optimizer_enabled",
            "depends_value": True,
            "order": 61,
        },
    )


# ==================== 自拍配置（合并自拍模式 + 自动自拍） ====================

class SelfieSection(PluginConfigBase):
    """自拍配置

    合并了原"自拍模式"和"自动自拍"两块配置。auto_ 前缀的字段属于自动自拍。
    """

    __ui_label__ = "自拍配置"
    __ui_icon__ = "camera"
    __ui_order__ = 3

    # ---- 自拍模式 ----
    enabled: bool = Field(
        default=True,
        description="是否启用自拍模式功能",
        json_schema_extra={"label": "启用自拍", "order": 1},
    )
    reference_image_path: str = Field(
        default="",
        description="自拍参考图片路径（相对插件目录或绝对路径）。配置后自动用图生图模式，模型不支持时自动回退",
        json_schema_extra={
            "label": "参考图片",
            "placeholder": "images/reference.png",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 2,
        },
    )
    prompt_prefix: str = Field(
        default="",
        description="自拍模式提示词前缀，用于添加 Bot 默认形象特征（发色、瞳色、服装等）",
        json_schema_extra={
            "label": "提示词前缀",
            "input_type": "textarea",
            "rows": 2,
            "placeholder": "blue hair, red eyes, school uniform, 1girl",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 3,
        },
    )
    negative_prompt: str = Field(
        default="",
        description="自拍基础负面提示词。所有风格自动附加手部质量负面提示词，standard 额外附加防双手拿手机提示词",
        json_schema_extra={
            "label": "负面提示词",
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
        description="是否启用日程增强自拍。开启后手动自拍会结合日程活动生成更贴合情境的场景（需安装 autonomous_planning 插件）",
        json_schema_extra={
            "label": "日程增强",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 5,
        },
    )
    default_style: Literal["standard", "mirror", "photo"] = Field(
        default="standard",
        description="自拍默认风格：standard(前置自拍) / mirror(对镜自拍) / photo(第三人称照片)",
        json_schema_extra={
            "label": "默认自拍风格",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 6,
        },
    )

    # ---- 自动自拍 ----
    auto_enabled: bool = Field(
        default=False,
        description="是否启用自动自拍。需同时安装 autonomous_planning（日程） + MaiTrace（QQ 空间，原 Maizone）。无日程数据时自动跳过",
        json_schema_extra={"label": "启用自动自拍", "order": 20},
    )
    interval_minutes: int = Field(
        default=120,
        ge=10,
        le=1440,
        description="自动自拍间隔（分钟），建议 60-240",
        json_schema_extra={
            "label": "自动自拍间隔",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 21,
        },
    )
    selfie_model: str = Field(
        default="",
        description="自动自拍使用的模型 ID。留空时依次回退到「普通自拍模型」、"
        "「默认图生图模型」、内置 model1。自动自拍必然携带参考图，应填图生图模型",
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
        description="普通自拍（聊天中用自然语言要自拍）使用的模型 ID。"
        "自拍必然携带参考图、走图生图，通常应填图生图模型。留空则依次回退到"
        "「默认图生图模型」→「默认文生图模型」",
        json_schema_extra={
            "label": "普通自拍模型",
            "hint": "留空=使用默认图生图模型；自拍走图生图，建议填图生图模型",
            "placeholder": "model2",
            "depends_on": "selfie.enabled",
            "depends_value": True,
            "order": 8,
        },
    )
    quiet_hours_start: str = Field(
        default="00:00",
        description="自动自拍安静时段开始（HH:MM），此时段内不发自拍",
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
        description="自动自拍安静时段结束（HH:MM）",
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
        description="是否为自动自拍生成配文",
        json_schema_extra={
            "label": "生成配文",
            "depends_on": "selfie.auto_enabled",
            "depends_value": True,
            "order": 25,
        },
    )


# ==================== 代理（独立） ====================

class ProxySection(PluginConfigBase):
    """代理设置"""

    __ui_label__ = "代理设置"
    __ui_icon__ = "globe"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="是否启用代理。开启后所有 API 请求通过代理服务器",
        json_schema_extra={"label": "启用代理", "order": 1},
    )
    url: str = Field(
        default="http://127.0.0.1:7890",
        description="代理服务器地址",
        json_schema_extra={
            "label": "代理地址",
            "hint": "支持 HTTP / HTTPS / SOCKS5",
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
        description="代理连接超时时间（秒）",
        json_schema_extra={
            "label": "超时时间",
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
        description="风格对应的 Stable Diffusion 提示词",
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
        default="魔搭潦草模型",
        description="模型显示名称",
        json_schema_extra={"label": "模型名称", "group": "connection", "order": 1},
    )
    base_url: str = Field(
        default="https://api-inference.modelscope.cn/v1",
        description="API 服务地址",
        json_schema_extra={
            "label": "API地址",
            "placeholder": "https://api.example.com/v1",
            "group": "connection",
            "order": 2,
        },
    )
    api_key: str = Field(
        default="Bearer xxxxxxxxxxxxxxxxxxxxxx",
        description="API 密钥（统一 'Bearer xxx' 格式）",
        json_schema_extra={
            "label": "API密钥",
            "input_type": "password",
            "placeholder": "Bearer sk-xxx",
            "group": "connection",
            "order": 3,
        },
    )
    format: Literal[
        "openai", "openai-chat", "gemini", "doubao", "modelscope",
        "shatangyun", "mengyuai", "comfyui", "dashscope",
    ] = Field(
        default="openai",
        description="API 格式",
        json_schema_extra={"label": "API格式", "group": "connection", "order": 4},
    )
    model: str = Field(
        default="cancel13/liaocao",
        description="模型标识",
        json_schema_extra={
            "label": "模型标识",
            "placeholder": "model-name / 0 / workflow.json",
            "group": "connection",
            "order": 5,
        },
    )
    fixed_size_enabled: bool = Field(
        default=False,
        description="是否固定图片尺寸",
        json_schema_extra={"label": "固定尺寸", "group": "params", "order": 6},
    )
    default_size: str = Field(
        default="1024x1024",
        description="默认图片尺寸",
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
        description="随机种子，-1 表示每次随机",
        json_schema_extra={"label": "随机种子", "group": "params", "order": 8},
    )
    guidance_scale: float = Field(
        default=2.5,
        ge=-1.0,
        le=20.0,
        description="引导强度（CFG）。填 -1 = 不发送该参数（用于不支持 guidance_scale 的模型，"
        "如豆包 Seedream 5.0 lite）；ComfyUI 类型不支持此语义，-1 会回退为 7",
        json_schema_extra={"label": "引导强度", "step": 0.5, "group": "params", "order": 9},
    )
    num_inference_steps: int = Field(
        default=20,
        ge=1,
        le=150,
        description="推理步数",
        json_schema_extra={"label": "推理步数", "group": "params", "order": 10},
    )
    watermark: bool = Field(
        default=True,
        description="是否添加水印",
        json_schema_extra={"label": "水印", "group": "params", "order": 11},
    )
    custom_prompt_add: str = Field(
        default=", Nordic picture book art style, minimalist flat design, liaocao",
        description="正面提示词增强",
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
        description="负面提示词",
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
        description="艺术家风格标签（砂糖云专用）",
        json_schema_extra={"label": "艺术家标签", "group": "prompts", "order": 14},
    )
    support_img2img: bool = Field(
        default=True,
        description="该模型是否支持图生图功能",
        json_schema_extra={"label": "支持图生图", "group": "prompts", "order": 15},
    )
    auto_recall_delay: int = Field(
        default=0,
        ge=0,
        le=120,
        description="自动撤回延时（秒）。大于 0 启用，0 不撤回。需先在「基础配置」中开启撤回总开关",
        json_schema_extra={"label": "撤回延时", "group": "prompts", "order": 16},
    )
    cfg: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="砂糖云专用：CFG Rescale",
        json_schema_extra={"label": "CFG Rescale", "step": 0.1, "group": "platform", "order": 20},
    )
    sampler: Literal[
        "k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
        "k_dpmpp_2m_sde", "k_dpmpp_2m", "k_dpmpp_sde",
    ] = Field(
        default="k_euler_ancestral",
        description="砂糖云专用：采样器",
        json_schema_extra={"label": "采样器", "group": "platform", "order": 21},
    )
    nocache: int = Field(
        default=0,
        ge=0,
        le=1,
        description="砂糖云专用：是否禁用缓存",
        json_schema_extra={"label": "禁用缓存", "group": "platform", "order": 22},
    )
    noise_schedule: Literal[
        "karras", "native", "exponential", "polyexponential",
    ] = Field(
        default="karras",
        description="砂糖云专用：噪声调度",
        json_schema_extra={"label": "噪声调度", "group": "platform", "order": 23},
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
        description="风格预设列表。每项含英文名、中文别名（逗号分隔）和 SD 提示词。可在 WebUI 增删",
        json_schema_extra={"label": "风格列表", "order": 1},
    )


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


# ==================== 根配置 ====================

class MaisArtConfig(PluginConfigBase):
    """麦麦绘卷根配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    basic: BasicSection = Field(default_factory=BasicSection)
    selfie: SelfieSection = Field(default_factory=SelfieSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    styles: StylesSection = Field(default_factory=StylesSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
