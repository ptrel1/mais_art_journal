"""控制台跨分区保存必须保留配置、校验失败原件和并发修改。"""

from copy import deepcopy
from pathlib import Path

import json
import pytest
import tomlkit

from mais_art_journal.core.config.model_editor import load_editor, save_editor
from mais_art_journal.plugin import MaisArtPlugin


@pytest.fixture
def editor(tmp_path: Path):
    path = tmp_path / "config.toml"
    config = MaisArtPlugin.build_default_config()
    config["models"]["items"][0].update(base_url="https://offline.invalid/v1", model="offline-model", api_key="existing-secret")
    config["proxy"]["url"] = "http://offline.invalid:7890"
    path.write_text("# Keep this comment\n" + tomlkit.dumps(config), encoding="utf-8")
    result = load_editor(path, MaisArtPlugin.build_config_schema())
    assert result["success"]
    payload = {name: deepcopy(result[name]) for name in ("items", "defaults", "settings")}
    return path, result, payload


def test_save_multiple_panels_preserves_models_other_settings_and_comments(editor):
    path, loaded, payload = editor
    payload["settings"]["basic"].update(prompt_optimizer_enabled=False, cache_max_size=17)
    payload["settings"]["selfie"].update(default_style="mirror", prompt_prefix="silver hair")
    payload["settings"]["group_image"]["group_ids"] = ["group-open-id"]
    payload["settings"]["styles"]["items"] = [{"name": "ink", "aliases": "ink-style", "prompt": "ink wash painting"}]
    payload["settings"]["quota"]["rules"] = [{"platform": "qq", "target_type": "group", "target_id": "group-open-id", "limit": 8, "period": ""}]
    saved = save_editor(path, json.dumps(payload), loaded["revision"])
    assert saved["success"], saved
    current = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    assert current["models"]["items"][0]["api_key"] == "existing-secret"
    assert current["proxy"]["url"] == "http://offline.invalid:7890"
    assert path.read_text(encoding="utf-8").startswith("# Keep this comment")
    assert current["basic"]["cache_max_size"] == 17
    assert current["selfie"]["default_style"] == "mirror"
    assert saved["settings"]["quota"]["rules"][0]["period"] is None
    reloaded = load_editor(path, MaisArtPlugin.build_config_schema())
    assert reloaded["settings"] == saved["settings"]


@pytest.mark.parametrize("section,patch", [
    ("basic", {"cache_max_size": 0}),
    ("group_image", {"min_messages": 31, "context_messages": 30}),
    ("quota", {"rules": [{"platform": "qq", "target_type": "group", "target_id": "x", "limit": 2}] * 2}),
    ("styles", {"items": [{"name": "same", "prompt": "a"}, {"name": "same", "prompt": "b"}]}),
    ("selfie", {"quiet_hours_start": "25:00"}),
    ("selfie", {"llm_selfie_model": "missing-model"}),
    ("selfie", {"selfie_model": "missing-model"}),
    ("group_image", {"model_id": "missing-model"}),
    ("comfyui", {"wait_seconds": 0}),
    ("proxy", {"timeout": 0}),
    ("basic", {"pic_command_model": "model999"}),
    ("plugin", {"enabled": False}),
])
def test_invalid_or_protected_settings_do_not_change_file(editor, section, patch):
    path, loaded, payload = editor
    original = path.read_bytes()
    payload["settings"].setdefault(section, {}).update(patch)
    saved = save_editor(path, json.dumps(payload), loaded["revision"])
    assert not saved["success"]
    assert path.read_bytes() == original


def test_conflicting_changes_are_preserved(editor):
    path, loaded, payload = editor
    path.write_bytes(path.read_bytes() + b"\n# Changed by another page\n")
    newer = path.read_bytes()
    payload["settings"]["basic"]["cache_enabled"] = False
    saved = save_editor(path, json.dumps(payload), loaded["revision"])
    assert saved["conflict"] and not saved["success"]
    assert path.read_bytes() == newer


def test_previous_model_only_payload_still_preserves_settings(editor):
    path, loaded, payload = editor
    payload.pop("settings")
    payload["items"][0]["name"] = "renamed"
    saved = save_editor(path, json.dumps(payload), loaded["revision"])
    assert saved["success"] and saved["settings"] == loaded["settings"]


def test_panels_expose_existing_config_fields_with_constraints(editor):
    _, loaded, _ = editor
    sections = {section["key"]: section for panel in loaded["panels"] for section in panel["sections"]}
    assert sections["basic_advanced"]["name"] == "basic"
    assert sections["basic_advanced"]["fields"]["cache_max_size"]["min"] == 1
    assert sections["selfie"]["fields"]["prompt_prefix"]["type"] == "textarea"
    assert sections["styles"]["fields"]["items"]["item_fields"]["prompt"]["type"] == "textarea"
    period = sections["quota"]["fields"]["rules"]["item_fields"]["period"]
    assert period["choices"] == ["", "daily", "weekly", "monthly", "once"]
    assert "plugin" not in loaded["settings"]


def test_proxy_and_comfyui_settings_round_trip(editor):
    path, loaded, payload = editor
    payload["settings"]["proxy"].update(enabled=True, url="http://offline.invalid:8080", timeout=90)
    payload["settings"]["comfyui"].update(client_id="second-bot", wait_seconds=450)
    saved = save_editor(path, json.dumps(payload), loaded["revision"])
    assert saved["success"], saved
    reloaded = load_editor(path, MaisArtPlugin.build_config_schema())
    assert reloaded["settings"]["proxy"] == payload["settings"]["proxy"]
    assert reloaded["settings"]["comfyui"] == payload["settings"]["comfyui"]
    assert path.read_text(encoding="utf-8").startswith("# Keep this comment")


def test_deleting_a_model_still_used_for_selfies_is_rejected(editor):
    path, _, _ = editor
    config = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    second = {**config["models"]["items"][0], "id": "model2"}
    config["models"]["items"].append(second)
    config["selfie"]["llm_selfie_model"] = "model2"
    path.write_text(tomlkit.dumps(config), encoding="utf-8")
    loaded = load_editor(path, MaisArtPlugin.build_config_schema())
    payload = {name: deepcopy(loaded[name]) for name in ("items", "defaults", "settings")}
    payload["items"] = [item for item in payload["items"] if item["id"] != "model2"]
    original = path.read_bytes()
    saved = save_editor(path, json.dumps(payload), loaded["revision"])
    assert not saved["success"] and "model2" in saved["error"]
    assert path.read_bytes() == original
