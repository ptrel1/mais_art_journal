"""在 Runner 重建配置前完成旧版绘卷的数据迁移。"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import os
import re
import tempfile

from maibot_sdk.config import extract_plugin_config_version, rebuild_plugin_config_data

import tomlkit

from .backup import backup_config_if_version_changed
from .models import BasicSection, MaisArtConfig, SelfieSection
from .novelai import NOVELAI_FIELD_NAMES, NovelAIConfig


def _version_key(version: str) -> Tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version)
    if match is None:
        raise ValueError(f"绘卷配置版本格式无效：{version!r}")
    return tuple(int(part) for part in match.groups())


def _merge_old_section(
    data: Dict[str, Any], old_name: str, target_name: str,
    fields: Mapping[str, str],
) -> None:
    old = data.pop(old_name, None)
    if old is None:
        return
    if not isinstance(old, dict):
        raise ValueError(f"旧配置 {old_name} 必须是配置节")
    target = data.setdefault(target_name, {})
    if not isinstance(target, dict):
        raise ValueError(f"配置 {target_name} 必须是配置节")
    for old_key, new_key in fields.items():
        if old_key in old:
            target.setdefault(new_key, old[old_key])


def _convert_legacy_sections(data: Dict[str, Any]) -> None:
    """迁移拆分前的配置节；已填写的新字段优先，布尔 false 和空列表照原值保留。"""
    basic_fields = {name: name for name in BasicSection.model_fields}
    basic_fields["default_model"] = "default_model"
    for section in ("generation", "components"):
        _merge_old_section(data, section, "basic", basic_fields)
    _merge_old_section(data, "cache", "basic", {
        "enabled": "cache_enabled", "max_size": "cache_max_size",
    })
    _merge_old_section(data, "auto_recall", "basic", {"enabled": "auto_recall_enabled"})
    _merge_old_section(data, "prompt_optimizer", "basic", {
        "enabled": "prompt_optimizer_enabled", "llm_task_name": "llm_task_name",
        "task_name": "llm_task_name",
    })
    selfie_fields = {name: name for name in SelfieSection.model_fields}
    selfie_fields.update({"enabled": "auto_enabled", "selfie_style": "default_style"})
    _merge_old_section(data, "auto_selfie", "selfie", selfie_fields)

    basic = data.get("basic", {})
    if not isinstance(basic, dict):
        raise ValueError("配置 basic 必须是配置节")
    old_default = basic.pop("default_model", None)
    new_default = basic.get("default_txt2img_model", "")
    if old_default is not None and isinstance(new_default, str) and not new_default.strip():
        basic["default_txt2img_model"] = old_default


def _convert_models(data: Dict[str, Any]) -> None:
    section = data.get("models")
    if section is None:
        return
    if isinstance(section, list):
        data["models"] = {"items": section}
        return
    if not isinstance(section, dict):
        raise ValueError("旧配置 models 必须是模型列表或配置节")
    if "items" in section:
        return
    items = []
    for model_id, model in section.items():
        if not isinstance(model, dict):
            raise ValueError(f"旧模型 {model_id!r} 必须是配置节")
        item = dict(model)
        item.setdefault("id", model_id)
        items.append(item)
    data["models"] = {"items": items}


def _convert_styles(data: Dict[str, Any]) -> None:
    aliases = data.pop("style_aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("旧配置 style_aliases 必须是配置节")
    section = data.get("styles")
    if section is None:
        return
    if isinstance(section, list):
        items = section
    elif isinstance(section, dict) and "items" in section:
        items = section["items"]
    elif isinstance(section, dict):
        items = [{"name": name, "prompt": prompt} for name, prompt in section.items()]
    else:
        raise ValueError("旧配置 styles 必须是风格列表或配置节")
    if not isinstance(items, list):
        raise ValueError("配置 styles.items 必须是风格列表")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("风格列表中的每项必须是配置节")
        if not item.get("aliases") and item.get("name") in aliases:
            item["aliases"] = aliases[item["name"]]
    data["styles"] = {"items": items}


def _assign_missing_model_ids(data: Dict[str, Any]) -> None:
    """原生列表新增项可留空编号；保存时分配未使用的编号，保留已有编号。"""
    section = data.get("models", {})
    if not isinstance(section, dict) or not isinstance(section.get("items", []), list):
        return  # 结构错误由后续配置校验报告。
    items = section.get("items", [])
    used = {item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
    next_number = 1
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id", "")
        if not isinstance(model_id, str) or model_id.strip():
            continue  # 非空重复编号和类型错误仍由配置校验报告。
        while f"model{next_number}" in used:
            next_number += 1
        item["id"] = f"model{next_number}"
        used.add(item["id"])
        next_number += 1


def _flatten_novelai_options(data: Dict[str, Any]) -> None:
    """在 SDK 移除旧字段之前保留 NAI 嵌套配置，新直接字段优先。"""
    section = data.get("models", {})
    if not isinstance(section, dict) or not isinstance(section.get("items", []), list):
        return
    for item in section.get("items", []):
        if not isinstance(item, dict) or "novelai" not in item:
            continue
        old = item.pop("novelai")
        # 只迁移用户已有的字段，缺省值随后由 SDK 统一补齐。
        NovelAIConfig.model_validate(old)
        for name, value in old.items():
            item.setdefault(NOVELAI_FIELD_NAMES[name], value)


def normalize_config_for_upgrade(
    config_data: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    """仅在配置入口转换旧结构；运行时始终使用当前版本的强类型配置。"""
    defaults = MaisArtConfig().model_dump(mode="python")
    if not config_data:
        return defaults, True
    current_version = extract_plugin_config_version(config_data)
    expected_version = extract_plugin_config_version(defaults)
    if _version_key(current_version) > _version_key(expected_version):
        raise ValueError(f"绘卷配置来自更新版本 {current_version}，不能降级到 {expected_version}")

    migrated = deepcopy(dict(config_data))
    _convert_legacy_sections(migrated)
    _convert_models(migrated)
    _assign_missing_model_ids(migrated)
    _flatten_novelai_options(migrated)
    _convert_styles(migrated)
    # 必须先改名和转结构，再交给 SDK 移除废弃字段、补齐默认值和更新版本号。
    rebuilt = rebuild_plugin_config_data(defaults, migrated)
    normalized = MaisArtConfig.model_validate(rebuilt).model_dump(mode="python")
    return normalized, normalized != config_data


def _toml_compatible(value: Any) -> Any:
    """TOML 没有 null；省略未指定的可选字段，读取时由配置模型恢复默认值。"""
    if isinstance(value, dict):
        return {key: _toml_compatible(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_toml_compatible(item) for item in value]
    return value


def _update_document(target: MutableMapping[str, Any], values: Dict[str, Any]) -> None:
    """尽量保留未变配置节的注释；原始完整文档另行逐字节备份。"""
    values = _toml_compatible(values)
    for key in list(target):
        if key not in values:
            del target[key]
    for key, value in values.items():
        existing = target.get(key)
        if isinstance(existing, MutableMapping) and isinstance(value, dict):
            _update_document(existing, value)
        elif key not in target or existing != value:
            target[key] = tomlkit.item(value)


def upgrade_config_file(config_path: Path) -> bool:
    """工厂创建时执行，早于宿主读文件；验证、备份、原子替换按顺序完成。"""
    try:
        original = config_path.read_bytes()
    except FileNotFoundError:
        return False
    document = tomlkit.parse(original.decode("utf-8-sig"))
    raw_config = document.unwrap()
    current_version = extract_plugin_config_version(raw_config)
    normalized, changed = normalize_config_for_upgrade(raw_config)
    if not changed:
        return False
    _update_document(document, normalized)
    content = tomlkit.dumps(document).encode("utf-8")
    backup_config_if_version_changed(
        str(config_path.parent), config_path.name,
        normalized["plugin"]["config_version"], current_version,
        force=True,
    )
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=config_path.parent, prefix=f".{config_path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, config_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True
