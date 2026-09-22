"""模型配置注册表

封装 plugin.config.models 列表的访问。从 v4.1.0 开始 models 是 List[ModelConfig]，
每个 ModelConfig 有自己的 id 字段。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..plugin import MaisArtPlugin


def _coerce_to_list(value: Any) -> List[Dict[str, Any]]:
    """把 plugin.config.models 的列表归一化为 list[dict]

    支持的输入形态：
    - list[ModelConfig | dict]: 正式形态（v4.1.1+，ModelsSection.items）
    - dict[str, ModelConfig | dict]: 旧 v4.0.0 [models.modelX] 形态
    """
    if isinstance(value, list):
        return [_dict_of(m) for m in value if m is not None]
    if isinstance(value, dict):
        out: List[Dict[str, Any]] = []
        for key, cfg in value.items():
            cfg_dict = _dict_of(cfg)
            cfg_dict.setdefault("id", str(key))
            out.append(cfg_dict)
        return out
    return []


def _dict_of(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return {}


def _read_models_items(plugin: "MaisArtPlugin") -> List[Dict[str, Any]]:
    """读取 plugin.config.models.items 列表，兼容多种历史形态"""
    section = plugin.get_config("models", None)
    if section is None:
        return []

    # 标准形态：dict 含 items 列表（v4.1.1+ ModelsSection）
    if isinstance(section, dict) and "items" in section:
        return _coerce_to_list(section.get("items"))

    # v4.1.0 中间形态：直接是 List[ModelConfig]
    # v4.0.0 旧形态：Dict[str, ModelConfig]
    return _coerce_to_list(section)


def list_models(plugin: "MaisArtPlugin") -> Dict[str, Dict[str, Any]]:
    """返回 {id: config_dict} 形式的所有模型"""
    items = _read_models_items(plugin)
    return {item["id"]: item for item in items if item.get("id")}


def resolve_txt2img_model(plugin: "MaisArtPlugin") -> str:
    """解析默认文生图模型 ID（留空时回退到旧 default_model，再回退到 model1）"""
    basic = plugin.config.basic
    model_id = (basic.default_txt2img_model or "").strip()
    if model_id:
        return model_id
    # 旧版配置只有 default_model，视为文生图模型
    legacy = (basic.default_model or "").strip()
    if legacy:
        return legacy
    return "model1"


def resolve_img2img_model(plugin: "MaisArtPlugin") -> str:
    """解析默认图生图模型 ID（留空时回退到文生图模型）"""
    basic = plugin.config.basic
    model_id = (basic.default_img2img_model or "").strip()
    if model_id:
        return model_id
    return resolve_txt2img_model(plugin)


def get_model_config(plugin: "MaisArtPlugin", model_id: Optional[str] = None) -> Dict[str, Any]:
    """按 id 查找模型配置；查不到时回退到默认模型；都没有返回 {}"""
    if not model_id:
        model_id = resolve_txt2img_model(plugin)

    items = list_models(plugin)
    cfg = items.get(model_id)
    if cfg:
        return cfg

    default_id = resolve_txt2img_model(plugin)
    if default_id and default_id != model_id:
        fallback = items.get(default_id)
        if fallback:
            return fallback
    return {}


def model_exists(plugin: "MaisArtPlugin", model_id: str) -> bool:
    if not model_id:
        return False
    return model_id in list_models(plugin)
