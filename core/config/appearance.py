"""独立模型页的外观与背景图片，和绘图模型配置分开保存。"""

from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Literal

import base64
import hashlib
import json
import os
import tempfile

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError


_LOCK = Lock()
MAX_IMAGE_BYTES = 300000  # Base64 后仍在宿主 512 KiB 页面返回预算内。


class Appearance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: Literal["dark", "light", "auto"] = "dark"
    preset: Literal["ink", "dawn", "forest"] = "ink"
    mode: Literal["gradient", "image"] = "gradient"
    overlay: int = Field(default=45, ge=0, le=85)
    blur: int = Field(default=0, ge=0, le=18)
    card_opacity: int = Field(default=88, ge=65, le=100)
    image_position: Literal["center", "top", "bottom"] = "center"
    image_file: str = Field(default="", pattern=r"^(?:background-[a-f0-9]{16}\.webp)?$")
    image_name: str = Field(default="", max_length=160)


def _read(directory: Path) -> Appearance:
    path = directory / "appearance.json"
    if not path.exists():
        return Appearance()
    return Appearance.model_validate_json(path.read_bytes())


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".appearance.", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _response(directory: Path, settings: Appearance) -> Dict[str, Any]:
    public = settings.model_dump(exclude={"image_file"})
    image_path = directory / settings.image_file if settings.image_file else None
    if image_path is not None and image_path.is_file():
        image_bytes = image_path.read_bytes()
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("已保存的背景图片过大，请重新上传。")
        public["image"] = "data:image/webp;base64," + base64.b64encode(image_bytes).decode("ascii")
    else:
        public["image"] = ""
        public["image_name"] = ""
        if public["mode"] == "image":
            public["mode"] = "gradient"
    return {"success": True, "appearance": public}


def load_appearance(directory: Path) -> Dict[str, Any]:
    with _LOCK:
        return _response(directory, _read(directory))


def save_appearance(directory: Path, payload: str) -> Dict[str, Any]:
    data = json.loads(payload)
    if not isinstance(data, dict) or set(data) - {"theme", "preset", "mode", "overlay", "blur", "card_opacity", "image_position"}:
        raise ValueError("外观设置无效。")
    with _LOCK:
        old = _read(directory)
        settings = Appearance.model_validate({**old.model_dump(), **data})
        if settings.mode == "image" and not settings.image_file:
            raise ValueError("请先上传一张背景图片。")
        response = _response(directory, settings)
        _atomic_write(directory / "appearance.json", settings.model_dump_json().encode("utf-8"))
        return response


def upload_background(directory: Path, source: Path, filename: str) -> Dict[str, Any]:
    """转成有界的 WebP 背景；只有完整图片就绪后才切换当前背景。"""
    with Image.open(source) as opened:
        if opened.width * opened.height > 40000000:
            raise ValueError("图片尺寸过大，请选择较小的图片。")
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((1920, 1920))
        content = b""
        for edge, quality in ((1920, 82), (1600, 72), (1280, 60), (960, 45)):
            image.thumbnail((edge, edge))
            output = BytesIO()
            image.save(output, format="WEBP", quality=quality, method=4)
            content = output.getvalue()
            if len(content) <= MAX_IMAGE_BYTES:
                break
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("图片压缩后仍过大，请选择另一张图片。")
    image_file = f"background-{hashlib.sha256(content).hexdigest()[:16]}.webp"
    with _LOCK:
        old = _read(directory)
        target = directory / image_file
        existed = target.exists()
        _atomic_write(target, content)
        settings = old.model_copy(update={
            "mode": "image", "image_file": image_file,
            "image_name": filename.replace("\\", "/").rsplit("/", 1)[-1][:160] or "我的背景图片",
        })
        try:
            _atomic_write(directory / "appearance.json", settings.model_dump_json().encode("utf-8"))
        except BaseException:
            if not existed:
                target.unlink(missing_ok=True)
            raise
        if old.image_file and old.image_file != image_file:
            (directory / old.image_file).unlink(missing_ok=True)
        return _response(directory, settings)


def reset_appearance(directory: Path, *, remove_image: bool = False) -> Dict[str, Any]:
    with _LOCK:
        old = _read(directory)
        settings = old.model_copy(update={"mode": "gradient", "image_file": "", "image_name": ""}) if remove_image else Appearance(image_file=old.image_file, image_name=old.image_name)
        _atomic_write(directory / "appearance.json", settings.model_dump_json().encode("utf-8"))
        if remove_image and old.image_file:
            (directory / old.image_file).unlink(missing_ok=True)
        return _response(directory, settings)


def appearance_error(error: Exception) -> Dict[str, Any]:
    if isinstance(error, ValidationError):
        message = "外观参数无效，请检查调节范围。"
    elif isinstance(error, (OSError, ValueError)):
        message = "外观或背景图片无法保存，请检查图片及插件数据目录。"
    else:
        message = "外观操作失败，请重试。"
    return {"success": False, "error": message}
