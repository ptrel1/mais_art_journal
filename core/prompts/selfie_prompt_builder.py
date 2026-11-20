"""从同一份身份、服装、场景和构图结构输出标签或自然语言自拍提示词。

三种模式只约束各自的镜头与持机方式；每次选择一种景别，
自由手保持自然，活动道具与日程地点一致。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import asyncio
import json
import logging
import re

from ..selfie.schedule_provider import ActivityInfo, classify_activity
from ..utils.prompt_policy import strip_prompt_weights, supports_reference_strength as supports_reference_strength, uses_weighted_tags
from .framing import requested_framing, resolve_selfie_style
from .scene_action_llm import build_scene_llm_prompt, generate_scene_with_llm, get_action_for_activity

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


logger = logging.getLogger("plugin.mais_art_journal.selfie_builder")


# 简单自然的动作池；无明确动作时保持放松，不强制复杂手势或额外道具。
_STANDARD_HAND_ACTIONS = [
    "free hand resting naturally at side",
    "free hand relaxed outside the frame",
    "gently adjusting hair with one hand",
    "one hand loosely resting on lap",
]
_MIRROR_HAND_ACTIONS = [
    "free hand resting naturally at side",
    "one hand loosely in pocket",
    "gently adjusting collar with one hand",
    "free hand resting on waist",
]
_PHOTO_HAND_ACTIONS = [
    "arms relaxed naturally at sides",
    "walking with a natural arm swing",
    "hands resting comfortably on lap",
    "one hand resting on a nearby surface",
]


# 不在固定模板里混入景别、地点或不属于活动的道具。
_STYLE_SCENE_PROMPTS = {
    "standard": "(front-camera selfie:1.2), arm-length viewpoint, phone and holding hand outside frame, face clearly visible",
    "mirror": "(mirror selfie:1.2), single coherent mirror reflection, phone visible only in reflection below face",
    "photo": "(third-person perspective:1.2), candid activity, natural perspective",
}
_STYLE_NEGATIVE_EXTRAS = {
    "standard": "visible phone, mirror selfie, full body selfie, two hands holding a phone",
    "mirror": "phone obscuring face, duplicate person outside reflection, front-camera selfie",
    "photo": "phone in hands, camera in hands, selfie stick, selfie pose, photographer, camera crew, visible photography equipment, studio lighting equipment",
}
_STYLE_ACTION_FALLBACKS = {
    "standard": "free hand resting naturally at side",
    "mirror": "free hand resting naturally at side",
    "photo": "hands resting naturally, relaxed posture",
}

# 局部取景必须描述实际裁切边界，避免“人物肖像”及参考图脸部把画面拉回半身。
_DETAIL_CROPS = {
    "lower-body portrait": (
        "Tightly crop the image from the waist down to the feet. Only the hips, legs and feet are inside the frame; "
        "the head, face, shoulders and chest are completely outside the frame."
    ),
    "leg detail": (
        "Make a tight close-up of the legs, cropped from mid-thigh to the ankles. The legs fill most of the image; "
        "the head, face, shoulders, chest and waist are completely outside the frame."
    ),
    "foot detail": (
        "Make a tight close-up of the feet, cropped from the ankles down. The feet fill most of the image; "
        "the head, face, torso, thighs and knees are completely outside the frame."
    ),
}
_DETAIL_NEGATIVES = {
    "lower-body portrait": "head in frame, face in frame, shoulders in frame, chest in frame, upper-body portrait, full-body portrait",
    "leg detail": "head in frame, face in frame, torso in frame, waist in frame, upper-body portrait, full-body portrait",
    "foot detail": "head in frame, face in frame, torso in frame, thighs in frame, knees in frame, full-body portrait",
}


# 清洗与镜头冲突的持机或双手动作；普通活动道具可以保留。
_PHONE_LIKE_PATTERN = re.compile(
    r"\b(phone|smartphone|cellphone|mobile\s+phone|camera|iphone|selfie\s+stick)\b|手机|相机|自拍杆|スマホ|カメラ",
    re.IGNORECASE,
)
_MULTI_HAND_PATTERN = re.compile(
    r"\b(both hands?|two hands?|two-handed|interlocked fingers|crossed arms?|arms\s+(?:stretched|spread|raised|wide|extended)|hands\s+(?:clasped|joined|together|holding))\b|双手|两只手|両手",
    re.IGNORECASE,
)
_SELFIE_POSE_PATTERN = re.compile(r"\b(arm extended|reaching toward camera|toward camera|selfie pose|taking selfie)\b|伸手.*镜头", re.IGNORECASE)
_LOWER_ACTION_PATTERN = re.compile(
    r"\b(?:legs?|thighs?|knees?|lap|feet|foot|ankles?|stockings?|pants|trousers|shorts|skirt)\b|腿|膝|脚|丝袜|长袜|裙|裤|足元",
    re.IGNORECASE,
)
_HEAD_ACTION_PATTERN = re.compile(r"\b(?:hair|head|face|eyes?|shoulders?|smil\w*)\b|头发|脸|眼|肩|微笑|髪|顔", re.IGNORECASE)
_ABOVE_FOOT_ACTION_PATTERN = re.compile(r"\b(?:hands?|arms?|knees?|thighs?|waist)\b|手|膝|大腿|腰", re.IGNORECASE)


def get_hand_actions_for_style(selfie_style: str) -> List[str]:
    if selfie_style == "mirror":
        return _MIRROR_HAND_ACTIONS.copy()
    if selfie_style == "photo":
        return _PHOTO_HAND_ACTIONS.copy()
    return _STANDARD_HAND_ACTIONS.copy()


def get_scene_prompt_for_style(selfie_style: str = "standard") -> str:
    return _STYLE_SCENE_PROMPTS.get(selfie_style, _STYLE_SCENE_PROMPTS["standard"])


def sanitize_hand_action_for_style(hand_action: Optional[str], selfie_style: str = "standard") -> Optional[str]:
    """按自拍风格清洗手部动作，避免注入冲突的持机姿态。"""
    if not hand_action or not hand_action.strip():
        return None
    cleaned = hand_action.strip()
    if _PHONE_LIKE_PATTERN.search(cleaned) or (
        selfie_style in ("standard", "mirror") and _MULTI_HAND_PATTERN.search(cleaned)
    ) or (selfie_style == "photo" and _SELFIE_POSE_PATTERN.search(cleaned)):
        return _STYLE_ACTION_FALLBACKS[selfie_style]
    return cleaned


def sanitize_action_for_framing(action: str, framing: str, style: str) -> str:
    """服装仍保留，动作不能要求展示取景范围外的身体部位。"""
    if framing in {"upper-body portrait", "waist-up portrait", "close portrait"} and _LOWER_ACTION_PATTERN.search(action):
        return _STYLE_ACTION_FALLBACKS[style]
    if framing in _DETAIL_CROPS and _HEAD_ACTION_PATTERN.search(action):
        return {"lower-body portrait": "standing naturally", "leg detail": "legs resting naturally", "foot detail": "feet resting naturally"}[framing]
    if framing == "foot detail" and _ABOVE_FOOT_ACTION_PATTERN.search(action):
        return "feet resting naturally"
    return action


def build_hand_prompt_for_style(hand_action: Optional[str], selfie_style: str = "standard") -> Optional[str]:
    """动作和持机约束各描述一次。"""
    cleaned = sanitize_hand_action_for_style(hand_action, selfie_style)
    if not cleaned:
        return None
    if selfie_style == "standard":
        return f"{cleaned}, other hand holding phone outside frame"
    if selfie_style == "mirror":
        return f"{cleaned}, other hand holding phone below face in mirror reflection"
    return cleaned


def get_negative_prompt_for_style(selfie_style: str, base_negative: str = "", *, framing: str = "") -> str:
    """组装简洁的手部质量和镜头专属负面词。"""
    return _dedupe_keywords(", ".join(filter(None, [
        base_negative, "bad anatomy, malformed hands, extra fingers, missing fingers, additional people, background people",
        _STYLE_NEGATIVE_EXTRAS.get(selfie_style, _STYLE_NEGATIVE_EXTRAS["standard"]),
        _DETAIL_NEGATIVES.get(framing, ""),
    ])))


async def generate_hand_action_with_llm(
    ctx: "PluginContext", description: str, selfie_style: str = "standard",
    llm_task: str = "utils", timeout_seconds: float = 45,
) -> Optional[str]:
    """复用场景 LLM prompt，只取 action 字段。"""
    try:
        system_prompt = await asyncio.to_thread(build_scene_llm_prompt, selfie_style)
        result = await ctx.llm.generate(
            prompt=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps({"activity": description, "requested_style": selfie_style})},
            ],
            model=llm_task, temperature=0.7, max_tokens=2048, timeout_ms=int(timeout_seconds * 1000),
        )
        if not isinstance(result, dict) or result.get("success") is False:
            return None
        response = result.get("response", result.get("content", ""))
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
        scene = json.loads(clean)
        action = scene.get("action")
        return action.strip() if isinstance(action, str) and action.strip() else None
    except Exception:
        logger.exception("手部动作 LLM 生成失败")
        return None


@dataclass(frozen=True)
class SelfieScene:
    identity: str
    outfit: str
    environment: str
    action: str
    expression: str
    lighting: str
    style: str
    framing: str
    visual_style: str = ""


@dataclass
class SelfiePromptResult:
    prompt: str
    negative_prompt: str
    selfie_style: str = "standard"
    outfit: str = ""
    photo_plan: Dict[str, str] = field(default_factory=dict)


def _dedupe_keywords(text: str) -> str:
    """按英文逗号去重关键词，保持首次出现顺序。"""
    seen: Set[str] = set()
    unique: List[str] = []
    for keyword in text.split(","):
        clean = keyword.strip()
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            unique.append(clean)
    return ", ".join(unique)


def _strip_weights(text: str) -> str:
    """保留旧内部入口，语法清洗与普通图共用同一实现。"""
    return strip_prompt_weights(text)


def _identity_without_outfit(appearance: str) -> str:
    """从旧外观前缀排除服装片段，避免参考服装压过活动换装。"""
    clothing = re.compile(
        r"\b(?:outfit|clothes|clothing|wear|attire|costume|garments?|dress|shirt|skirt|uniform|jacket|coat|pants|trousers|"
        r"pajamas|swimwear|sportswear|apron|shoes|socks|stockings|hat|hoodie|sweater|bathrobe)\b|"
        r"衣|服|裙|裤|鞋|袜|帽|围裙|睡袍|女仆装|泳装", re.IGNORECASE,
    )
    parts = []
    for part in re.split(r"[,，;；\n]", appearance):
        identity = re.split(r"\b(?:wearing|dressed in|clad in)\b|穿着|身穿", part, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if identity and not clothing.search(identity) and identity.lower() != "solo":
            parts.append(identity)
    return ", ".join(parts)


def _choose_framing(style: str, environment: str, action: str = "", description: str = "") -> str:
    """按拍摄意图和动作选择单一景别，保留道具与活动所需的画面空间。"""
    explicit = requested_framing(description)
    if explicit:
        return explicit
    text = f"{environment} {action} {description}".lower()
    context_framing = requested_framing(f"{environment} {action}")
    full_body = context_framing == "full-body portrait"
    close = context_framing == "close portrait"
    if close and not full_body:
        return "close portrait"
    if style == "standard":
        # 手臂长度的前置镜头保留肩膀和自由手，不强行塞入全身景别。
        return "upper-body portrait"
    if style == "mirror":
        full_length = any(word in environment.lower() for word in ("full-length", "floor mirror", "dressing room", "全身镜", "穿衣镜", "试衣"))
        return "full-body portrait" if full_length else "waist-up portrait"
    movement = any(word in text for word in ("walking", "running", "stretching", "dancing", "yoga", "散步", "跑步", "伸展", "跳舞", "瑜伽"))
    return "full-body environmental portrait" if full_body or movement else "medium environmental portrait"


def _render_tags(scene: SelfieScene) -> str:
    identity = _visible_identity(scene)
    crop = _DETAIL_CROPS.get(scene.framing, "")
    return _dedupe_keywords(", ".join(filter(None, [
        "solo character", "one person only", identity, f"wearing {scene.outfit}",
        scene.environment, build_hand_prompt_for_style(scene.action, scene.style),
        "" if crop else scene.expression, scene.lighting, scene.framing, crop, get_scene_prompt_for_style(scene.style),
        "background limited to the described setting and ordinary scene objects",
        scene.visual_style,
    ])))


def _visible_identity(scene: SelfieScene) -> str:
    """脸和头发不在局部裁切内；只强调能出现在画面里的身份特征。"""
    if scene.framing not in _DETAIL_CROPS:
        return scene.identity
    face_features = re.compile(r"\b(?:hair|hairstyle|eyes?|face|ears?|nose|mouth|lips?|head)\b|头发|发色|脸|眼|耳", re.IGNORECASE)
    return ", ".join(part.strip() for part in re.split(r"[,，;；]", scene.identity) if not face_features.search(part))


def _render_natural(scene: SelfieScene) -> str:
    """直接编排场景结构，避免把长串 SD 标签作为自然语言提示词。"""
    crop = _DETAIL_CROPS.get(scene.framing, "")
    sentences = [f"{crop} Required framing: {scene.framing}."] if crop else []
    if scene.framing == "upper-body portrait":
        sentences.append("Frame only the face, shoulders and upper torso, with the lower image edge at the waist. The legs and feet stay outside the frame even when the outfit includes stockings or trousers.")
    sentences.append("Create a single coherent image of one character, using the visual medium requested in the scene description; otherwise retain the reference art style when provided.")
    sentences.append("There is exactly one person in the entire image. The background contains only the specified setting and ordinary scene objects, with no other people.")
    if crop:
        sentences.append("Use the reference image only to match the visible body proportions, skin tone and character design. Do not copy its framing or widen the crop to include its face or whole figure.")
    else:
        sentences.append("Preserve the character's face shape, hairstyle, hair color, eye color and identity from the reference image when provided.")
    sentences.append("Use the reference for character design and art style only; do not copy its background or add people and props absent from the specified setting.")
    visible_identity = _visible_identity(scene)
    if visible_identity:
        identity = re.sub(r"\b1girl\b", "female character", _strip_weights(visible_identity), flags=re.IGNORECASE)
        identity = re.sub(r"\b1boy\b", "male character", identity, flags=re.IGNORECASE)
        sentences.append(f"The character's stable appearance is {identity}.")
    if scene.visual_style:
        sentences.append(f"Use the requested visual medium and art style: {_strip_weights(scene.visual_style)}.")
    sentences.extend([
        f"Dress the character in {_strip_weights(scene.outfit)}, replacing the reference clothing.",
        f"Use this setting: {_strip_weights(scene.environment)}.",
        f"The action and posture are {_strip_weights(scene.action)}.",
        f"Use {_strip_weights(scene.lighting)}.",
    ])
    if not crop:
        sentences.append(f"The expression is {_strip_weights(scene.expression)}.")
        sentences.append(f"Compose a single {scene.framing} with a readable subject and enough room for the activity and its props.")
        if scene.framing.startswith("full-body"):
            sentences.append("The entire character must fit inside the image from the top of the head to both feet; keep both feet visible within the bottom edge.")
    if scene.style == "standard":
        sentences.append("The finished image is the direct view captured by a front camera: an arm-length self-portrait with a clear face. It is not an observer's picture of someone taking a selfie. The capturing device and the holding hand stay completely outside the frame, beyond every image edge. No visible phone, camera, screen, mirror or reflection; only the free hand may appear for gestures or props.")
    elif scene.style == "mirror":
        sentences.append("Show only the character's reflection in a mirror appropriate to this setting. One hand holds the phone below the face, visible only in the reflection. Follow the described action with the free hand. Keep the face clear, the reflection coherent and the reflected environment consistent with this location.")
    else:
        sentences.append("Use a natural third-person perspective of the subject, with the viewing point outside the image. This viewpoint does not introduce any visible recording device or people observing the subject. Keep the character's hands free of phones and cameras, and keep all recording and lighting equipment outside the image.")
        if not crop:
            sentences.append("Show a candid moment of the activity; both hands may follow the described action or use ordinary activity props.")
    if crop:
        sentences.append("Keep the specified close crop as the final composition. Do not include the excluded body regions to show identity, expression, the setting or activity props.")
    return " ".join(sentences)


async def build(
    *, ctx: "PluginContext", selfie_style: str, description: str, free_hand_action: str = "",
    activity_scene: Optional[Dict[str, Any]] = None, bot_appearance: str = "",
    base_negative: str = "", log_prefix: str = "[selfie_builder]", run_scene_optimizer: bool = True,
    llm_task: str = "utils", activity_info: Optional[ActivityInfo] = None,
    activity_rule: Optional[Dict[str, str]] = None, outfit: str = "", api_format: str = "openai",
    prompt_syntax: str = "auto", timeout_seconds: float = 45, recent_styles: Optional[List[str]] = None,
) -> SelfiePromptResult:
    """场景生成负责换装和动作，固定身份优先使用配置，再输出标签或自然语言。"""
    activity = activity_info or ActivityInfo(classify_activity(description), description)
    # 先决定能完成用户取景要求的视角，再让场景 LLM 构造动作与持机约束。
    environment_hint = "; ".join((activity.location, activity.scene, activity.description))
    if activity_scene is not None:
        environment_hint += "; " + activity_scene.get("environment", "")
    selfie_style = resolve_selfie_style(selfie_style, description, environment_hint)
    deterministic_scene = activity_scene is None and not run_scene_optimizer
    if activity_scene is None:
        if run_scene_optimizer:
            activity_scene = await generate_scene_with_llm(
                ctx, activity, selfie_style, llm_task=llm_task, outfit=outfit,
                bot_appearance=bot_appearance, activity_rule=activity_rule,
                timeout_seconds=timeout_seconds, recent_styles=recent_styles, photo_description=description,
            )
            if activity_scene is None:
                raise RuntimeError("自拍场景生成失败，已取消本次生图")
        else:
            activity_scene = get_action_for_activity(activity, activity_rule=activity_rule, recent_styles=recent_styles)
            activity_scene["character_identity"] = _identity_without_outfit(bot_appearance)
    style = activity_scene["selfie_style"] if selfie_style == "auto" else selfie_style
    if style not in {"standard", "mirror", "photo"}:
        raise ValueError(f"无效的自拍风格: {style}")
    # 关闭优化器或调用方提供场景时，同样不能保留与明确景别冲突的旧风格。
    style = resolve_selfie_style(style, description, activity_scene.get("environment", "") or environment_hint)
    selected_outfit = outfit or activity.outfit or activity_scene["outfit"]
    # 显式动作 > 本次活动动作 > 自然放松；不再强制随机手势。
    action = free_hand_action or ("" if deterministic_scene else activity_scene.get("hand_action")) or _STYLE_ACTION_FALLBACKS[style]
    if deterministic_scene and not free_hand_action:
        action = (
            "free hand naturally positioned for the described activity"
            if style in {"standard", "mirror"}
            else "natural posture, hands positioned appropriately for the described activity"
        )
    action = sanitize_hand_action_for_style(action, style) or _STYLE_ACTION_FALLBACKS[style]
    environment = activity_scene.get("environment") or activity.location or activity.description
    if deterministic_scene:
        # 关闭优化器时保留用户原场景，固定映射只提供衣服/动作等缺省值。
        # 不能把“树下读书”变成图书馆，或把晚间休息固定成午后客厅。
        environment = "; ".join(dict.fromkeys(part.strip() for part in (
            activity.location, activity.scene, activity.description, description,
        ) if part.strip()))
    if activity.location and activity.location not in environment:
        environment = f"{activity.location}; {environment}"
    framing = _choose_framing(style, environment, action, description)
    visible_action = sanitize_action_for_framing(action, framing, style)
    if visible_action != action:
        logger.info("%s 动作与景别 %s 冲突，调整为画面内可展示的动作：%s -> %s", log_prefix, framing, action, visible_action)
    # 配置是固定身份的权威来源，不能让场景 LLM 的概括覆盖族裔、年龄等特征。
    # 服装仍单独清理，局部取景仍由 _visible_identity 排除画面外的特征。
    identity_source = bot_appearance if bot_appearance.strip() else activity_scene.get("character_identity", "")
    scene = SelfieScene(
        identity=_identity_without_outfit(identity_source),
        outfit=selected_outfit, environment=environment, action=visible_action,
        expression="natural expression consistent with the described moment" if deterministic_scene else activity_scene.get("expression", "natural expression"),
        lighting="natural ambient lighting appropriate to the setting" if deterministic_scene else activity_scene.get("lighting", "natural ambient lighting"),
        style=style, framing=framing,
        visual_style=activity_scene.get("visual_style", ""),
    )
    tags = uses_weighted_tags(api_format, prompt_syntax)
    prompt = _render_tags(scene) if tags else _render_natural(scene)
    negative = get_negative_prompt_for_style(style, base_negative, framing=scene.framing)
    if not tags:
        negative = _strip_weights(negative)
    logger.info("%s 自拍提示词已构造：style=%s，framing=%s", log_prefix, style, scene.framing)
    return SelfiePromptResult(prompt, negative, style, selected_outfit, asdict(scene))


async def build_for_activity(
    *, ctx: "PluginContext", activity_info: ActivityInfo, selfie_style: str = "standard",
    bot_appearance: str = "", base_negative: str = "", log_prefix: str = "[auto_selfie_builder]",
    llm_task: str = "utils", activity_rule: Optional[Dict[str, str]] = None,
    api_format: str = "openai", prompt_syntax: str = "auto", timeout_seconds: float = 45,
    recent_styles: Optional[List[str]] = None,
) -> Optional[SelfiePromptResult]:
    """自动自拍依赖场景 LLM；失败返回 None，由调用方跳过。"""
    scene = await generate_scene_with_llm(
        ctx, activity_info, selfie_style, llm_task=llm_task, bot_appearance=bot_appearance,
        outfit=activity_info.outfit, activity_rule=activity_rule, timeout_seconds=timeout_seconds,
        recent_styles=recent_styles,
    )
    if scene is None:
        return None
    return await build(
        ctx=ctx, selfie_style=selfie_style, description=activity_info.description,
        activity_info=activity_info, activity_scene=scene, bot_appearance=bot_appearance,
        base_negative=base_negative, log_prefix=log_prefix, run_scene_optimizer=False,
        llm_task=llm_task, activity_rule=activity_rule, api_format=api_format,
        prompt_syntax=prompt_syntax, timeout_seconds=timeout_seconds, recent_styles=recent_styles,
    )
