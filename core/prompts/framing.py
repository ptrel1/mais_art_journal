"""识别明确的取景要求，并保持景别与实际拍摄方式一致。"""

from typing import List, Optional

import re

_FULL_BODY = re.compile(r"\b(?:full[\s_-]*body|head[\s-]*to[\s-]*toe|full[\s-]*length\s+(?:photo|portrait|shot))\b|全身(?![镜鏡])(?:像|照)?", re.IGNORECASE)
_LOWER_BODY = re.compile(r"\blower[\s-]*body\b|下半身", re.IGNORECASE)
_FEET = re.compile(r"\b(?:feet|foot)\b|脚部|足部|足元|(?:拍|画|展示|看看|特写)[^。\n]{0,20}(?:脚|双足)", re.IGNORECASE)
_LEGS = re.compile(r"\b(?:legs?|thighs?|knees?)\b(?![\s-]*(?:high\b|warmers?\b|length\b))|腿部|双腿|膝盖|脚のアップ|(?:拍|画|展示|看看|特写)[^。\n]{0,20}腿(?!袜|裤)", re.IGNORECASE)
_FACE = re.compile(r"\b(?:close[\s-]*up|close portrait|face detail)\b|脸部特写|面部特写|顔のアップ", re.IGNORECASE)
_FULL_LENGTH_MIRROR = re.compile(r"full[\s-]*length\s+mirror|floor mirror|全身镜|穿衣镜|姿見", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(?:no|without|avoid|exclude|not|don't|do not)\b(?:\s+(?:show(?:ing)?|include|including|draw(?:ing)?|take|a|any|the|your)){0,3}\s*$|"
    r"(?:不要|不需要|不用|别|不拍|不展示|不显示)(?:拍|展示|显示|露出|看|画)?(?:你的|我的|她的|他的|角色的)?$",
    re.IGNORECASE,
)


def requested_framing(description: str) -> Optional[str]:
    """全身优先；腿、脚特写分别处理，服装名称本身不要求扩大景别。"""
    for pattern, framing in (
        (_FULL_BODY, "full-body portrait"), (_LOWER_BODY, "lower-body portrait"),
        (_FEET, "foot detail"), (_LEGS, "leg detail"), (_FACE, "close portrait"),
    ):
        for match in pattern.finditer(description):
            if not _NEGATED.search(description[:match.start()]):
                return framing
    return None


def needs_body_view(description: str) -> bool:
    return requested_framing(description) in {"full-body portrait", "lower-body portrait", "leg detail", "foot detail"}


def resolve_selfie_style(style: str, description: str, environment: str) -> str:
    """前置近距离自拍不能满足身体取景；没有合适镜子时使用外部镜头。"""
    if needs_body_view(description) and (
        style == "standard" or (style == "mirror" and (
            requested_framing(description) != "full-body portrait" or not _FULL_LENGTH_MIRROR.search(environment)
        ))
    ):
        return "photo"
    return style


def compatible_styles(styles: List[str], description: str, environment: str) -> List[str]:
    return [style for style in styles if resolve_selfie_style(style, description, environment) == style]
