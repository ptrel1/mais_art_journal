"""贯穿图片生成流水线的请求对象。

每次生图调用（Tool / Command 风格 / Command 自然语言 / 自动自拍 / 独立接口）
都会构造一个 GenerationRequest，沿着 Pipeline 的 14 个 Step 流转，
中间状态共享在同一对象上，行为开关控制每个 Step 是否短路。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional


SourceLiteral = Literal[
    "tool",          # @Tool draw_picture 智能生图
    "cmd_style",     # /dv <风格名>
    "cmd_natural",   # /dv <自然语言描述>
    "auto_selfie",   # 自动自拍后台任务
    "standalone",    # generate_image_standalone 外部调用
]

SelfieStyleLiteral = Literal["auto", "standard", "mirror", "photo"]


@dataclass
class GenerationRequest:
    """图片生成请求（贯穿流水线）"""

    # ==================== 输入参数 ====================
    description: str = ""
    model_id: str = ""
    size: str = ""
    strength: Optional[float] = None
    input_image_base64: Optional[str] = None
    extra_negative_prompt: Optional[str] = None

    # 自拍相关
    is_selfie: bool = False
    selfie_style: SelfieStyleLiteral = "standard"
    free_hand_action: str = ""
    activity_scene: Optional[Dict[str, Any]] = None
    activity_type: str = ""
    activity_description: str = ""
    outfit: str = ""
    activity_location: str = ""
    activity_time_window: str = ""
    activity_as_of: str = ""
    activity_timezone: str = ""
    activity_scene_description: str = ""
    recent_selfie_styles: list[str] = field(default_factory=list)

    # ==================== 行为开关（各 Step 自查短路） ====================
    send_image: bool = True
    update_cache: bool = True
    schedule_recall: bool = True
    debug_info: bool = True
    silent_img2img_fallback: bool = False
    """图生图回退时是否静默：True=不发"将为您生成新图片"消息（自拍参考图回退用）"""

    # ==================== 上下文 ====================
    stream_id: str = ""
    chat_id: str = ""
    user_id: str = ""
    group_id: str = ""
    platform: str = ""
    log_prefix: str = "[MaisArt]"
    command_message: Any = None
    source: SourceLiteral = "tool"

    # ==================== 中间状态（Step 间共享） ====================
    resolved_model_config: Optional[Dict[str, Any]] = None
    api_format: str = ""
    selfie_negative_prompt: Optional[str] = None
    llm_chosen_size: Optional[str] = None
    final_size: str = ""
    is_img2img: bool = False
    raw_api_result: Any = None
    parsed_image_data: Optional[str] = None
    resolved_image_data: Optional[str] = None
    send_timestamp: float = 0.0
    sent_message_id: str = ""
    cache_hit: bool = False
    # 进入客户端后可能已提交并计费；失败或取消不能重新放行同一用户请求。
    generation_started: bool = False
    # 只有 SDK 明确返回结果才能确认投递；取消/异常期间保留 unknown，供额度结算。
    delivery_state: Literal["not_sent", "unknown", "sent"] = "not_sent"
    quota_reservation_id: str = ""

    # ==================== 输出 ====================
    success: bool = False
    error: Optional[str] = None
    user_message: Optional[str] = None
    """成功/失败时同步发送给用户的消息文案（已发送或将由 pipeline 发送）"""

    # ==================== 调试 / 额外字段 ====================
    extras: Dict[str, Any] = field(default_factory=dict)
