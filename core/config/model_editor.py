"""绘卷控制台的数据与保存接口；各分区沿用原有配置路径。"""

from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any, Dict
from urllib.parse import urlsplit

import hashlib
import json
import os
import tempfile

from pydantic import ValidationError

import tomlkit

from .backup import backup_config_if_version_changed
from .control_panel import merge_settings, settings_panels, settings_values
from .migration import _update_document, normalize_config_for_upgrade
from .models import ModelConfig
from .ui_schema import CHOICE_MEANINGS, MODEL_TEXT


DEFAULT_FIELDS = ("default_txt2img_model", "default_img2img_model", "pic_command_model")
_SAVE_LOCK = Lock()


class ModelEditorError(ValueError):
    """仅包含可公开给用户的错误提示，不携带原始配置。"""

# 未列出的字段是所有格式共用的参数；保留完整值，只在页面隐藏无关控件。
FIELD_FORMATS = {
    "support_img2img": ["openai", "openai-chat", "doubao", "modelscope", "novelai", "mengyuai", "comfyui", "gemini", "dashscope"],
    "negative_prompt_add": ["openai", "openai-chat", "modelscope", "shatangyun", "novelai", "mengyuai", "comfyui", "dashscope"],
    "seed": ["openai", "openai-chat", "doubao", "modelscope", "shatangyun", "novelai", "mengyuai", "comfyui", "dashscope"],
    "guidance_scale": ["openai", "doubao", "modelscope", "shatangyun", "novelai", "mengyuai", "comfyui"],
    "num_inference_steps": ["openai", "modelscope", "shatangyun", "novelai", "mengyuai", "comfyui"],
    "watermark": ["openai", "doubao", "dashscope"],
    "artist": ["novelai", "shatangyun"],
    "selfie_reference_strength": ["openai", "novelai", "comfyui"],
    "cfg": ["novelai", "shatangyun"],
    "sampler": ["novelai", "shatangyun"],
    "noise_schedule": ["novelai", "shatangyun"],
    "nocache": ["shatangyun"],
    "img2img_model_index": ["mengyuai"],
    "image_upload_url": ["mengyuai"],
    "endpoint_path": ["dashscope"],
    "dashscope_async": ["dashscope"],
    "prompt_extend": ["dashscope"],
    **{name: ["novelai"] for name in ModelConfig.model_fields if name.startswith("novelai_")},
}


def editor_fields(schema: Dict[str, Any]) -> Dict[str, Any]:
    fields = deepcopy(schema["sections"]["models"]["fields"]["items"]["item_fields"])
    for name, field in fields.items():
        info = ModelConfig.model_fields[name]
        extra = info.json_schema_extra or {}
        field["label"], field["hint"] = MODEL_TEXT.get(name, (field["label"], info.description or ""))
        field["group"] = extra.get("group", "params")
        field["formats"] = FIELD_FORMATS.get(name)
        field["choice_labels"] = CHOICE_MEANINGS.get(name, {})
        field["type"] = extra.get("input_type", field["type"])
        if field["type"] == "integer":
            field["step"] = 1
        elif field["type"] == "number":
            field["step"] = extra.get("step", "any")
        for constraint in info.metadata:
            # Pydantic 的数值约束是不同的 metadata 类型。
            if (minimum := getattr(constraint, "ge", None)) is not None:
                field["min"] = minimum
            if (maximum := getattr(constraint, "le", None)) is not None:
                field["max"] = maximum
    return fields


def _read_config(path: Path) -> tuple[Dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = tomlkit.parse(raw.decode("utf-8-sig")).unwrap() if raw else None
    normalized, _ = normalize_config_for_upgrade(data)
    return normalized, raw


def _validation_message(error: ValidationError) -> str:
    return "；".join(
        f"{'.'.join(str(part) for part in item['loc'])}：{item['msg']}"
        for item in error.errors(include_input=False, include_url=False)[:5]
    )


def load_editor(path: Path, schema: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with _SAVE_LOCK:
            config, raw = _read_config(path)
    except ValidationError as error:
        return {"success": False, "error": _validation_message(error)}
    except (ValueError, TypeError):
        return {"success": False, "error": "配置文件无法读取，请先修正插件配置。"}
    return {
        "success": True,
        "revision": hashlib.sha256(raw).hexdigest(),
        "items": config["models"]["items"],
        "defaults": {name: config["basic"][name] for name in DEFAULT_FIELDS},
        "fields": editor_fields(schema),
        "new_model": {**ModelConfig().model_dump(), "id": "", "base_url": "", "model": ""},
        "settings": settings_values(config),
        "panels": settings_panels(schema),
    }


def save_editor(path: Path, payload: str, revision: str) -> Dict[str, Any]:
    """校验后备份并原子保存；版本冲突时保留用户当前表单，不覆盖其他修改。"""
    try:
        if len(payload.encode("utf-8")) > 60000:
            raise ModelEditorError("模型配置过大，请减少模型数量或提示词长度。")
        patch = json.loads(payload)
        if not isinstance(patch, dict) or not {"items", "defaults"}.issubset(patch) or set(patch) - {"items", "defaults", "settings"}:
            raise ModelEditorError("模型配置结构无效。")
        if not isinstance(patch["items"], list) or not patch["items"]:
            raise ModelEditorError("请至少保留一个模型。")
        if not isinstance(patch["defaults"], dict) or set(patch["defaults"]) != set(DEFAULT_FIELDS):
            raise ModelEditorError("默认模型设置不完整。")
        for index, item in enumerate(patch["items"], 1):
            if not isinstance(item, dict) or set(item) - set(ModelConfig.model_fields):
                raise ModelEditorError(f"第 {index} 个模型包含无效字段。")
            address = item.get("base_url", "")
            if not isinstance(address, str):
                raise ModelEditorError(f"第 {index} 个模型的服务地址无效。")
            url = urlsplit(address.strip())
            if url.scheme not in {"http", "https"} or not url.netloc:
                raise ModelEditorError(f"第 {index} 个模型请填写完整的 http:// 或 https:// 服务地址。")
            item["base_url"] = address.strip()
            if not isinstance(item.get("model"), str) or not item["model"].strip():
                raise ModelEditorError(f"第 {index} 个模型请填写服务商模型名称或工作流路径。")

        with _SAVE_LOCK:
            config, raw = _read_config(path)
            if hashlib.sha256(raw).hexdigest() != revision:
                return {"success": False, "error": "配置已在其他页面修改。请重新加载后再编辑保存。", "conflict": True}
            config["models"]["items"] = patch["items"]
            config["basic"].update(patch["defaults"])
            if "settings" in patch:
                merge_settings(config, patch["settings"])
            normalized, _ = normalize_config_for_upgrade(config)
            ids = {item["id"] for item in normalized["models"]["items"]}
            for name in DEFAULT_FIELDS:
                model_id = normalized["basic"][name]
                if (name == "pic_command_model" or model_id) and model_id not in ids:
                    raise ModelEditorError("请选择列表中存在的默认模型。")
            if not normalized["basic"]["default_txt2img_model"] and "model1" not in ids:
                raise ModelEditorError("请选择默认文生图模型。")
            for section, name, label in (
                ("selfie", "llm_selfie_model", "聊天自拍使用的模型"),
                ("selfie", "selfie_model", "主动自拍使用的模型"),
                ("group_image", "model_id", "群话题图片使用的模型"),
            ):
                model_id = normalized[section][name].strip()
                if model_id and model_id not in ids:
                    raise ModelEditorError(f"「{label}」引用了不存在的模型 {model_id}，请重新选择或留空。")
            # 合并整个文档以保留注释和模型页以外的设置。
            document = tomlkit.parse(raw.decode("utf-8-sig")) if raw else tomlkit.document()
            _update_document(document, normalized)
            content = tomlkit.dumps(document).encode("utf-8")
            if content != raw:
                version = normalized["plugin"]["config_version"]
                backup_config_if_version_changed(str(path.parent), path.name, version, version, force=True)
                temporary_path = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".model-config.", suffix=".tmp", delete=False) as temporary:
                        temporary_path = Path(temporary.name)
                        temporary.write(content)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    # 写临时文件期间若磁盘配置改变，仍拒绝覆盖。
                    current = path.read_bytes() if path.exists() else b""
                    if current != raw:
                        return {"success": False, "error": "保存期间配置发生变化，请重新加载。", "conflict": True}
                    os.replace(temporary_path, path)
                    temporary_path = None
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
            return {
                "success": True, "revision": hashlib.sha256(content).hexdigest(),
                "items": normalized["models"]["items"],
                "defaults": {name: normalized["basic"][name] for name in DEFAULT_FIELDS},
                "settings": settings_values(normalized),
            }
    except ValidationError as error:
        return {"success": False, "error": _validation_message(error)}
    except ModelEditorError as error:
        return {"success": False, "error": str(error)}
    except (ValueError, TypeError):
        # 不返回 JSON 解码器及类型异常里的原始输入，避免带出密钥。
        return {"success": False, "error": "配置无效，请检查地址、模型名称、编号和参数范围。"}
