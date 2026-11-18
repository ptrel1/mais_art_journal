"""聊天生图额度配置；后台分享和跨插件生图使用各自的调度限制。"""

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo


QuotaPeriod = Literal["daily", "weekly", "monthly", "once"]


class QuotaRule(PluginConfigBase):
    """按平台及群/用户覆盖默认额度。"""

    platform: str = Field(default="qq", pattern=r"^\S+$", description="平台标识", json_schema_extra={"label": "平台"})
    target_type: Literal["group", "user"] = Field(
        default="group", description="group 为整个群共享，user 仅用于该用户的私聊",
        json_schema_extra={"label": "目标类型"},
    )
    target_id: str = Field(default="", pattern=r"^\S+$", description="宿主实际群 ID 或私聊用户 ID；官方 QQ 未绑定时填 OpenID，绑定后填统一 ID，与管理员/允许群配置规则相同", json_schema_extra={"label": "目标 ID"})
    limit: int = Field(default=20, ge=0, le=1000000, description="每周期张数，0 表示禁止生图", json_schema_extra={"label": "额度"})
    period: Optional[QuotaPeriod] = Field(
        default=None, description="留空沿用全局周期；daily 每天、weekly 每周、monthly 每月、once 一次性总额",
        json_schema_extra={"label": "周期"},
    )

    @field_validator("period", mode="before")
    @classmethod
    def normalize_empty_period(cls, value: Optional[str]) -> Optional[str]:
        """原生列表表单用空字符串表示留空，配置中统一保存为 None。"""
        return None if value == "" else value


class QuotaSection(PluginConfigBase):
    """仅限制用户通过命令或智能 Tool 发起的聊天生图。"""

    __ui_label__ = "聊天生图额度"
    __ui_icon__ = "gauge"
    __ui_order__ = 6

    enabled: bool = Field(
        default=False, description="启用聊天生图额度；自动自拍、群聊主动图片及 MaiTrace 配图不占用聊天额度",
        json_schema_extra={"label": "启用额度", "order": 1},
    )
    period: QuotaPeriod = Field(
        default="daily", description="daily 每天、weekly 每周一、monthly 每月一日重置；once 为一次性总额",
        json_schema_extra={"label": "默认周期", "depends_on": "quota.enabled", "depends_value": True, "order": 2},
    )
    timezone: str = Field(
        default="Asia/Shanghai", description="额度周期的 IANA 时区，如 Asia/Shanghai",
        json_schema_extra={"label": "周期时区", "depends_on": "quota.enabled", "depends_value": True, "order": 3},
    )
    group_limit: int = Field(
        default=20, ge=0, le=1000000, description="每群共享的默认张数，0 表示禁止群内生图",
        json_schema_extra={"label": "每群额度", "depends_on": "quota.enabled", "depends_value": True, "order": 4},
    )
    private_limit: int = Field(
        default=5, ge=0, le=1000000, description="每个私聊用户的默认张数，0 表示禁止私聊生图",
        json_schema_extra={"label": "私聊额度", "depends_on": "quota.enabled", "depends_value": True, "order": 5},
    )
    rules: List[QuotaRule] = Field(
        default_factory=list, description="针对指定群或私聊用户覆盖默认额度，同一平台目标只能配置一条规则",
        json_schema_extra={"label": "单独额度", "depends_on": "quota.enabled", "depends_value": True, "order": 6},
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"无效的额度时区：{value}") from exc
        return value

    @model_validator(mode="after")
    def unique_rules(self) -> "QuotaSection":
        keys = [(rule.platform, rule.target_type, rule.target_id) for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("额度规则的同一平台和目标不能重复")
        return self
