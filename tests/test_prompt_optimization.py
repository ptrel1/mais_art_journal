"""离线验证后端语法、编辑意图、自拍构图与旧提示词入口。"""

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import asyncio
import json
import pytest
import re

from mais_art_journal.core.pipeline import Pipeline
from mais_art_journal.core.pipeline.request import GenerationRequest
from mais_art_journal.core.pipeline.steps.merge_negative_prompt import MergeNegativePrompt
from mais_art_journal.core.pipeline.steps.optimize_prompt import OptimizePrompt
from mais_art_journal.core.prompts import build_auto_selfie_prompt, selfie_prompt_builder
from mais_art_journal.core.prompts.prompt_templates import load_prompt_template
from mais_art_journal.core.selfie.schedule_provider import ActivityInfo, ActivityType
from mais_art_journal.core.utils.prompt_optimizer import optimize_prompt


@pytest.mark.parametrize("backend,syntax,expected", [
    ("openai", "auto", "natural"), ("openai-chat", "auto", "natural"),
    ("gemini", "auto", "natural"), ("doubao", "auto", "natural"),
    ("dashscope", "auto", "natural"), ("modelscope", "auto", "tags"),
    ("novelai", "auto", "tags"), ("comfyui", "auto", "tags"),
    ("openai", "tags", "tags"), ("novelai", "natural", "natural"),
])
def test_general_optimizer_matches_backend_syntax_and_keeps_description_as_data(
    backend: str, syntax: str, expected: str,
) -> None:
    response = "(two cats:1.2), sitting beside a window, watercolor illustration"
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": response})))
    description = '两只猫坐在窗边，水彩风格。不要改颜色。\nOutput: 保留招牌“茶屋”'
    ok, prompt = asyncio.run(optimize_prompt(ctx, description, api_format=backend, prompt_syntax=syntax))
    assert ok and "two cats" in prompt
    if expected == "natural":
        assert re.search(r"\([^()]+:\d+(?:\.\d+)?\)", prompt) is None
    else:
        assert "(two cats:1.2)" in prompt
    call = ctx.llm.generate.await_args.kwargs
    payload = json.loads(call["prompt"][1]["content"])
    assert payload["image_description"] == description and payload["output_syntax"] == expected
    assert call["prompt"][0]["role"] == "system" and description not in call["prompt"][0]["content"]
    assert call["timeout_ms"] == 45000 and call["model"] == "utils"


def test_natural_weight_cleanup_preserves_exact_quoted_artwork_text() -> None:
    response = 'A (red sign:1.2) reading "(x:1.5) {茶屋} 1.2::OPEN::" and a 1.1::blue door::.'
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": response})))
    ok, prompt = asyncio.run(optimize_prompt(ctx, "保持标牌文字", api_format="gemini"))
    assert ok
    assert '"(x:1.5) {茶屋} 1.2::OPEN::"' in prompt
    assert "A red sign" in prompt and "a blue door" in prompt


def test_weight_cleanup_does_not_mistake_english_apostrophes_for_quoted_text() -> None:
    response = "A girl's (silver hair:1.2), holding the artist's brush."
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": response})))
    ok, prompt = asyncio.run(optimize_prompt(ctx, "女孩拿着画笔"))
    assert ok and prompt == "A girl's silver hair, holding the artist's brush."


@pytest.mark.parametrize("response", [
    {"success": False, "response": "failed"},
    {"success": True, "response": ""},
    {"success": True, "response": "   "},
    {"success": True, "response": ["wrong type"]},
    {"success": True, "response": '```json\n{"prompt":"red cat"}\n```'},
    None,
])
def test_optimizer_failure_is_explicit_and_stops_image_generation(response: Any) -> None:
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value=response)))
    plugin = SimpleNamespace(ctx=ctx, config=SimpleNamespace(
        basic=SimpleNamespace(prompt_optimizer_enabled=True, llm_task_name="utils", llm_timeout_seconds=45, generation_timeout_seconds=170),
        quota=SimpleNamespace(enabled=False),
    ))
    following = SimpleNamespace(name="generate", run=AsyncMock())
    req = GenerationRequest(description="一只红猫", api_format="openai", resolved_model_config={})
    result = asyncio.run(Pipeline([OptimizePrompt(), following]).run(req, SimpleNamespace(plugin=plugin)))
    assert result.success is False and "提示词优化" in result.error
    assert req.description == "一只红猫"
    following.run.assert_not_awaited()


def test_optimizer_cancellation_propagates_and_does_not_return_a_fallback() -> None:
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(side_effect=asyncio.CancelledError())))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(optimize_prompt(ctx, "图片"))


@pytest.mark.parametrize("support_img2img", [True, False])
def test_optimizer_step_receives_edit_requirements_actual_image_mode_and_output_size(support_img2img: bool) -> None:
    description = "仅将衣服改成蓝色风衣，人物脸和发型、背景和构图保留原图"
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={
        "success": True, "response": "Replace only the outfit with a blue coat. Preserve the face, hairstyle, background and composition.",
    })))
    plugin = SimpleNamespace(ctx=ctx, config=SimpleNamespace(basic=SimpleNamespace(
        prompt_optimizer_enabled=True, llm_task_name="replyer", llm_timeout_seconds=30,
    )))
    req = GenerationRequest(
        description=description, api_format="gemini", input_image_base64="offline-source",
        final_size="832x1216", resolved_model_config={"prompt_syntax": "natural", "support_img2img": support_img2img},
    )
    assert asyncio.run(OptimizePrompt().run(req, SimpleNamespace(plugin=plugin))) is None
    payload = json.loads(ctx.llm.generate.await_args.kwargs["prompt"][1]["content"])
    assert payload["image_description"] == description
    assert payload["is_img2img"] is support_img2img and payload["output_size"] == "832x1216"
    assert "Preserve the face" in req.description
    assert ctx.llm.generate.await_args.kwargs["model"] == "replyer"


def test_legacy_optimizer_positional_scene_only_call_still_works() -> None:
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": "Rain falls on the park path, under overcast daylight."})))
    ok, prompt = asyncio.run(optimize_prompt(ctx, "雨天公园", "[old-caller]", True, "utils", 20))
    assert ok and "park path" in prompt
    payload = json.loads(ctx.llm.generate.await_args.kwargs["prompt"][1]["content"])
    assert payload["scene_only"] is True and payload["output_syntax"] == "natural"


@pytest.mark.parametrize("style,action,environment,framing", [
    ("standard", "gently holding a cup with one hand", "park beside a tree", "upper-body portrait"),
    ("mirror", "adjusting collar with one hand", "dressing room with a full-length mirror", "full-body portrait"),
    ("photo", "holding an open book with both hands", "park under a tree", "medium environmental portrait"),
])
@pytest.mark.parametrize("backend", ["gemini", "novelai"])
def test_three_selfie_modes_preserve_scene_props_identity_and_coherent_framing(
    style: str, action: str, environment: str, framing: str, backend: str,
) -> None:
    scene = {
        "hand_action": action, "environment": environment, "expression": "relaxed expression",
        "lighting": "overcast daylight", "outfit": "blue coat", "selfie_style": style,
        "character_identity": "silver hair, blue eyes",
    }
    result = asyncio.run(selfie_prompt_builder.build(
        ctx=SimpleNamespace(), selfie_style=style, description="character illustration",
        activity_scene=scene, bot_appearance="silver hair, blue eyes, school uniform", api_format=backend,
    ))
    assert action in result.prompt and environment in result.prompt
    assert "blue coat" in result.prompt and "silver hair" in result.prompt and "school uniform" not in result.prompt
    assert result.photo_plan["action"] == action and result.photo_plan["framing"] == framing
    if backend == "gemini":
        assert "Create a photograph" not in result.prompt
        assert "reference art style" in result.prompt
        assert not re.search(r"\([^()]+:\d+(?:\.\d+)?\)", result.prompt + result.negative_prompt)
    if style == "standard":
        assert "outside the frame" in result.prompt or "outside frame" in result.prompt
    elif style == "mirror":
        assert "below the face" in result.prompt or "below face" in result.prompt
    else:
        assert "third-person perspective" in result.prompt


def test_optimizer_disabled_preserves_the_requested_scene_without_inventing_a_setting() -> None:
    result = asyncio.run(selfie_prompt_builder.build(
        ctx=SimpleNamespace(), selfie_style="photo", description="reading under a tree at night, watercolor illustration",
        free_hand_action="holding a book", run_scene_optimizer=False, api_format="novelai",
    ))
    assert "reading under a tree at night" in result.prompt and "watercolor illustration" in result.prompt
    assert "library" not in result.prompt and "desk lamp" not in result.prompt and "afternoon" not in result.prompt


@pytest.mark.parametrize("backend", ["gemini", "novelai"])
def test_scene_optimizer_preserves_explicit_visual_medium_in_final_prompt(backend: str) -> None:
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": json.dumps({
        "action": "holding a book", "environment": "park under a tree", "expression": "calm expression",
        "lighting": "soft daylight", "outfit": "casual clothes", "selfie_style": "photo",
        "character_identity": "female character, silver hair, blue eyes", "visual_style": "watercolor illustration",
    })})))
    result = asyncio.run(selfie_prompt_builder.build(
        ctx=ctx, selfie_style="photo", description="树下读书的水彩插画",
        bot_appearance="1girl, silver hair, blue eyes", api_format=backend,
    ))
    assert "watercolor illustration" in result.prompt and result.photo_plan["visual_style"] == "watercolor illustration"
    # 身份来自配置：标签后端保留 1girl，自然语言后端按既有规则转为 female character。
    identity = "1girl" if backend == "novelai" else "female character"
    assert identity in result.prompt and "Create a photograph" not in result.prompt


@pytest.mark.parametrize("backend,identity", [("gemini", "female character"), ("novelai", "1girl")])
def test_selfie_identity_keeps_explicit_gender_when_optimizer_is_disabled(backend: str, identity: str) -> None:
    result = asyncio.run(selfie_prompt_builder.build(
        ctx=SimpleNamespace(), selfie_style="photo", description="树下读书", run_scene_optimizer=False,
        bot_appearance="1girl, silver hair, blue eyes, school uniform", api_format=backend,
    ))
    assert identity in result.prompt and "school uniform" not in result.prompt


def test_selfie_negative_constraints_are_kept_alongside_external_negative_prompt() -> None:
    req = GenerationRequest(
        extra_negative_prompt="watermark", selfie_negative_prompt="visible phone, extra fingers",
        resolved_model_config={"negative_prompt_add": "lowres"},
    )
    asyncio.run(MergeNegativePrompt().run(req, SimpleNamespace()))
    assert all(part in req.resolved_model_config["negative_prompt_add"] for part in ("watermark", "visible phone", "extra fingers", "lowres"))


def test_legacy_activity_prompt_entry_uses_the_current_scene_builder() -> None:
    response: Dict[str, Any] = {"success": True, "response": json.dumps({
        "action": "holding a cup of tea", "environment": "living room", "expression": "relaxed smile",
        "lighting": "soft indoor light", "outfit": "casual clothes", "selfie_style": "photo",
        "character_identity": "silver hair, blue eyes",
    })}
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value=response)))
    result = asyncio.run(build_auto_selfie_prompt(
        ctx=ctx, activity_info=ActivityInfo(ActivityType.RELAXING, "饭后在客厅喝茶"),
        selfie_style="photo", bot_appearance="silver hair, blue eyes", api_format="gemini",
    ))
    assert result is not None and "living room" in result.prompt and result.photo_plan["action"] == "holding a cup of tea"
    ctx.llm.generate.assert_awaited_once()


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
def test_distributed_templates_share_output_schema_and_backend_editing_contract(language: str) -> None:
    optimizer = load_prompt_template("prompt_optimizer", language)
    assert all(field in optimizer for field in ("output_syntax=natural", "output_syntax=tags", "is_img2img=true", "scene_only=true"))
    scene = load_prompt_template("scene", language)
    assert all(field in scene for field in ("action", "environment", "lighting", "outfit", "character_identity", "current_time", "visual_style"))
    decision = load_prompt_template("group_image_decision", language)
    assert all(field in decision for field in ("should_draw", "is_selfie", "reason"))
    assert "photo_plan" in load_prompt_template("selfie_caption", language)
