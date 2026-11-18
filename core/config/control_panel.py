"""独立控制台的配置分区、表单约束与可保存字段。"""

from copy import deepcopy
from enum import Enum
from typing import Any, Dict, get_args

from .models import MaisArtConfig
from .ui_schema import CHOICE_MEANINGS


PANELS = (
    ("common", "常用设置", "设置聊天画图、提示词优化、缓存、等待时间和网络代理。", ("basic", "basic_advanced", "proxy", "comfyui")),
    ("styles", "风格预设", "保存常用画风，用名称或别名直接触发画图命令。", ("styles",)),
    ("selfie", "自拍与分享", "管理角色形象、拍照方式、允许群和日程分享。", (
        "selfie", "selfie_chat", "group_image_shared", "selfie_auto", "group_image",
        "selfie_advanced", "group_image_advanced",
    )),
    ("quota", "次数限制", "设置群聊共享次数、私聊次数和指定目标的单独规则。", ("quota",)),
)
EDITABLE_FIELDS = {
    section: set(MaisArtConfig.model_fields[section].annotation.model_fields)
    for section in ("basic", "styles", "selfie", "group_image", "quota", "proxy", "comfyui")
}
EDITABLE_FIELDS["basic"] -= {"default_txt2img_model", "default_img2img_model", "pic_command_model"}


def settings_values(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        section: {name: deepcopy(value) for name, value in config[section].items() if name in fields}
        for section, fields in EDITABLE_FIELDS.items()
    }


def merge_settings(config: Dict[str, Any], patch: Any) -> None:
    """只允许控制台声明的配置字段，不接收插件元信息或额外模型字段。"""
    if not isinstance(patch, dict) or set(patch) - set(EDITABLE_FIELDS):
        raise ValueError("控制台设置包含无效分区。")
    for section, values in patch.items():
        if not isinstance(values, dict) or set(values) - EDITABLE_FIELDS[section]:
            raise ValueError("控制台设置包含无效字段。")
        config[section].update(deepcopy(values))


def _decorate_field(name: str, definition: Dict[str, Any], info: Any) -> Dict[str, Any]:
    field = deepcopy(definition)
    extra = info.json_schema_extra or {}
    if extra.get("input_type"):
        field["type"] = extra["input_type"]
    choice_key = "selfie_style" if name == "default_style" else name
    field["choice_labels"] = CHOICE_MEANINGS.get(choice_key, {})
    if field["type"] == "integer":
        field["step"] = 1
    elif field["type"] == "number":
        field["step"] = extra.get("step", "any")
    for constraint in info.metadata:
        for attribute, key in (("ge", "min"), ("le", "max"), ("min_length", "min_length"),
                               ("max_length", "max_length"), ("pattern", "pattern")):
            if (value := getattr(constraint, attribute, None)) is not None:
                field[key] = value
    if field.get("item_fields"):
        item_model = get_args(info.annotation)[0]
        field["item_fields"] = {
            child: _decorate_field(child, child_field, item_model.model_fields[child])
            for child, child_field in field["item_fields"].items()
        }
        if name == "rules" and "period" in field["item_fields"]:
            field["item_fields"]["period"].update(
                choices=["", "daily", "weekly", "monthly", "once"],
                choice_labels={"": "沿用全局", **CHOICE_MEANINGS["period"]},
            )
    elif field["type"] == "array":
        item_type = get_args(info.annotation)[0]
        if isinstance(item_type, type) and issubclass(item_type, Enum):
            field["item_choices"] = [item.value for item in item_type]
            field["choice_labels"] = CHOICE_MEANINGS.get("activity_type", {})
    return field


def settings_panels(schema: Dict[str, Any]) -> list[Dict[str, Any]]:
    panels = []
    for panel_id, title, description, section_keys in PANELS:
        sections = []
        for key in section_keys:
            section = schema["sections"][key]
            model = MaisArtConfig.model_fields[section["name"]].annotation
            sections.append({
                **deepcopy(section), "key": key,
                "fields": {
                    name: _decorate_field(name, field, model.model_fields[name])
                    for name, field in section["fields"].items()
                    if name in EDITABLE_FIELDS[section["name"]] and not field.get("hidden")
                },
            })
        panels.append({"id": panel_id, "title": title, "description": description, "sections": sections})
    return panels
