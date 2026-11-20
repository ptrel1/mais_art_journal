"""在同一次场景生成中决定动作、服装和构图；关闭优化器时使用明确的活动规则。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

import asyncio
import json
import logging
import re

from ..selfie.activity_rules import ACTIVITY_OUTFITS, ACTIVITY_STYLES, choose_auto_style, style_candidates
from .framing import compatible_styles, requested_framing, resolve_selfie_style
from .prompt_templates import load_prompt_template

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext

    from ..selfie.schedule_provider import ActivityInfo

logger = logging.getLogger("plugin.mais_art_journal.scene_llm")


# ==================== 确定性映射（关闭 LLM 场景优化时使用） ====================

ACTIVITY_ACTIONS: Dict[str, str] = {
    "sleeping": "lying down, hugging pillow, cozy",
    "waking_up": "stretching, yawning, messy hair",
    "eating": "holding chopsticks, eating",
    "working": "typing on laptop, focused",
    "studying": "holding book, reading",
    "exercising": "stretching, athletic, holding water bottle",
    "relaxing": "lying on couch, relaxed, listening to music",
    "socializing": "making peace sign, happy, laughing",
    "commuting": "holding bag, walking, wearing earbuds",
    "hobby": "engaged in a creative activity, relaxed posture",
    "self_care": "adjusting hair with one hand",
    "cooking": "holding a spatula, preparing food",
    "shopping": "holding shopping bag, relaxed pose",
    "gaming": "holding game controller, playing a game",
    "bathing": "standing in a fully covered bathrobe, relaxed pose",
    "other": "standing, casual pose, natural",
}

ACTIVITY_ENVIRONMENTS: Dict[str, str] = {
    "sleeping": "bedroom, dim lighting, cozy atmosphere, bed",
    "waking_up": "bedroom, morning light, curtains, warm sunlight",
    "eating": "dining room, table setting",
    "working": "office desk, computer screen",
    "studying": "library, bookshelves, desk lamp",
    "exercising": "gym, fitness equipment",
    "relaxing": "living room, sofa, afternoon sun",
    "socializing": "outdoor cafe, bright atmosphere",
    "commuting": "city street, urban",
    "hobby": "art studio, creative space",
    "self_care": "bathroom, mirror, vanity",
    "cooking": "kitchen, stove, cooking utensils",
    "shopping": "clothing store, fitting mirror",
    "gaming": "living room, television, game console",
    "bathing": "bathroom, warm indoor atmosphere",
    "other": "indoor, natural lighting",
}

ACTIVITY_EXPRESSIONS: Dict[str, str] = {
    "sleeping": "peaceful expression, closed eyes",
    "waking_up": "drowsy expression, half-open eyes",
    "eating": "happy expression, enjoying food",
    "working": "focused expression, serious",
    "studying": "focused, thoughtful expression",
    "exercising": "energetic expression, determined",
    "relaxing": "relaxed smile, content",
    "socializing": "bright smile, happy",
    "commuting": "calm expression",
    "hobby": "excited, passionate",
    "self_care": "gentle smile, self-care",
    "cooking": "focused smile",
    "shopping": "cheerful expression",
    "gaming": "focused, excited expression",
    "bathing": "relaxed expression",
    "other": "natural smile",
}

ACTIVITY_LIGHTING: Dict[str, str] = {
    "sleeping": "dim warm light, night lamp",
    "waking_up": "soft morning light, golden hour",
    "eating": "warm indoor lighting",
    "working": "office lighting, even illumination",
    "studying": "desk lamp, focused light",
    "exercising": "bright natural light",
    "relaxing": "soft afternoon light, warm ambient light",
    "socializing": "bright cheerful lighting",
    "commuting": "morning sunlight",
    "hobby": "creative studio lighting",
    "self_care": "bathroom lighting, mirror reflection",
    "cooking": "warm kitchen lighting",
    "shopping": "bright indoor lighting",
    "gaming": "soft ambient light",
    "bathing": "warm bathroom lighting",
    "other": "natural lighting",
}


# ==================== LLM 场景生成 prompt（按风格约束） ====================

def build_scene_llm_prompt(selfie_style: str) -> str:
    """组装带构图约束的中文模板；英文、日文模板同步随插件分发。"""
    if selfie_style not in {"auto", "standard", "mirror", "photo"}:
        raise ValueError(f"无效的自拍构图: {selfie_style}")
    return load_prompt_template("scene").replace("{{requested_style}}", selfie_style)


# ==================== 公共 API ====================

async def generate_scene_with_llm(
    ctx: "PluginContext",
    activity_info: "ActivityInfo",
    selfie_style: str = "standard",
    llm_task: str = "utils",
    *,
    outfit: str = "",
    bot_appearance: str = "",
    activity_rule: Optional[Dict[str, str]] = None,
    timeout_seconds: float = 45,
    recent_styles: Optional[List[str]] = None,
    photo_description: str = "",
) -> Optional[Dict[str, str]]:
    """一次 LLM 调用同时生成场景、服装、身份特征和构图。

    失败显式返回 None，由调用方终止生成，不发布与当前活动不一致的图片。
    """
    try:
        description = photo_description or activity_info.description
        environment = "; ".join((activity_info.location, activity_info.scene, activity_info.description))
        selfie_style = resolve_selfie_style(selfie_style, description, environment)
        allowed_styles = compatible_styles(
            style_candidates(activity_info) if selfie_style == "auto" else [selfie_style],
            description, environment,
        )
        system_prompt = await asyncio.to_thread(build_scene_llm_prompt, selfie_style)
        rule = get_action_for_activity(activity_info, activity_rule=activity_rule, recent_styles=recent_styles)
        payload = {
            "activity_type": activity_info.activity_type.value,
            "activity": activity_info.description,
            "location": activity_info.location,
            "scene": activity_info.scene,
            "time_window": activity_info.time_window,
            "as_of": activity_info.as_of,
            "timezone": activity_info.timezone,
            "current_time": activity_info.current_time(),
            "photo_description": photo_description,
            "requested_style": selfie_style,
            "activity_style": rule["selfie_style"] if rule["selfie_style"] in allowed_styles else "photo",
            "allowed_styles": allowed_styles,
            "requested_framing": requested_framing(description) or "",
            "recent_styles": (recent_styles or [])[-6:],
            "outfit": outfit or activity_info.outfit or rule["outfit"],
            "character_appearance": bot_appearance,
        }

        result = await ctx.llm.generate(
            prompt=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model=llm_task, temperature=0.7, max_tokens=2048,
            timeout_ms=int(timeout_seconds * 1000),
        )

        success, response, model_name = False, "", ""
        if isinstance(result, dict):
            success = bool(result.get("success", True))
            response = str(result.get("response") or result.get("content") or "")
            model_name = str(result.get("model") or result.get("model_name") or "")

        if not (success and response):
            logger.warning("LLM 场景生成返回空响应")
            return None

        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        scene = json.loads(cleaned)
        required_keys = {"action", "environment", "expression", "lighting", "outfit", "selfie_style", "character_identity"}
        if not isinstance(scene, dict):
            raise ValueError("场景 LLM 必须返回 JSON 对象")
        # 七字段旧结构继续接受；新模板将媒介/画风独立输出，避免在环境描述中丢失。
        allowed_keys = required_keys | {"visual_style"}
        if not required_keys.issubset(scene) or set(scene) - allowed_keys:
            logger.warning("LLM 场景字段不符合约定：缺少=%s，多余=%s", required_keys - set(scene), set(scene) - allowed_keys)
            return None

        for key in required_keys:
            if not isinstance(scene[key], str) or len(scene[key]) > 1000 or (key != "character_identity" and not scene[key].strip()):
                logger.warning(f"LLM 场景字段 {key} 无效: {scene.get(key)}")
                return None

        style = scene["selfie_style"].strip()
        if style not in payload["allowed_styles"]:
            raise ValueError(f"场景构图 {style!r} 不符合本轮场景允许的构图")
        # 最终固定身份由自拍构造器从配置提取；LLM 省略身份不能阻止有效场景生成。
        visual_style = scene.get("visual_style", "")
        if not isinstance(visual_style, str) or len(visual_style) > 300:
            raise ValueError("场景 LLM 返回无效画风")
        logger.info(f"LLM 场景生成成功 (模型: {model_name}): style={style}")
        return {
            "hand_action": scene["action"],
            "environment": scene["environment"],
            "expression": scene["expression"],
            "lighting": scene["lighting"],
            "outfit": outfit or activity_info.outfit or scene["outfit"].strip(),
            "selfie_style": style,
            "character_identity": scene["character_identity"].strip(),
            "visual_style": visual_style.strip(),
        }

    except json.JSONDecodeError as e:
        logger.warning(f"LLM 场景 JSON 解析失败: {e}")
        return None
    except Exception as e:
        logger.exception(f"LLM 场景生成异常: {e}")
        return None


def get_action_for_activity(
    activity_info: "ActivityInfo", *, activity_rule: Optional[Dict[str, str]] = None,
    recent_styles: Optional[List[str]] = None,
) -> Dict[str, str]:
    """关闭 LLM 优化时使用的确定性场景，服装与构图遵循同一配置来源。"""
    key = activity_info.activity_type.value
    return {
        "hand_action": ACTIVITY_ACTIONS.get(key, ACTIVITY_ACTIONS["other"]),
        "environment": activity_info.location or ACTIVITY_ENVIRONMENTS.get(key, ACTIVITY_ENVIRONMENTS["other"]),
        "expression": ACTIVITY_EXPRESSIONS.get(key, ACTIVITY_EXPRESSIONS["other"]),
        "lighting": ACTIVITY_LIGHTING.get(key, ACTIVITY_LIGHTING["other"]),
        "outfit": activity_info.outfit or (activity_rule["outfit"] if activity_rule else ACTIVITY_OUTFITS[activity_info.activity_type]),
        "selfie_style": choose_auto_style(activity_info, activity_rule["selfie_style"] if activity_rule else ACTIVITY_STYLES[activity_info.activity_type], recent_styles),
    }
