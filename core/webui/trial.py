"""绘图后台任务；页面只收取短状态响应和有界图片分片。"""

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

import asyncio
import base64
import binascii
import logging
import math
import time
import uuid

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from ...plugin import MaisArtPlugin

logger = logging.getLogger("plugin.mais_art_journal.trial")
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 32 * 1024 * 1024
CHUNK_SIZE = 240000  # Base64 分片独立落在宿主 512 KiB 返回预算内。
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    model_id: str = Field(default="", max_length=100)
    size: str = Field(default="", max_length=30, pattern=r"^(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4})?$")
    negative_prompt: str = Field(default="", max_length=1000)
    reference_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    strength: float = Field(default=0.7, ge=0.1, le=1)
    selfie_mode: bool = False
    selfie_style: Literal["auto", "standard", "mirror", "photo"] = "auto"


@dataclass
class TrialJob:
    id: str
    request: TrialRequest
    started_at: float = field(default_factory=time.monotonic)
    state: str = "running"
    finished_at: Optional[float] = None
    error: str = ""
    model_id: str = ""
    size: str = ""
    is_img2img: bool = False
    image_base64: str = ""
    mime: str = ""

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id, "state": self.state, "error": self.error,
            "elapsed_seconds": round((self.finished_at or time.monotonic()) - self.started_at, 1),
            "model_id": self.model_id, "size": self.size, "is_img2img": self.is_img2img,
            "mime": self.mime, "chunks": math.ceil(len(self.image_base64) / CHUNK_SIZE),
            "request": self.request.model_dump(),
        }


def _validate_image(data: bytes, *, reference: bool = False) -> str:
    if not data or len(data) > (MAX_REFERENCE_BYTES if reference else MAX_RESULT_BYTES):
        raise ValueError("参考图不能超过 20 MB。" if reference else "生成图片超过 32 MB，无法在绘图页面预览。")
    with Image.open(BytesIO(data)) as image:
        allowed = {"PNG", "JPEG", "WEBP"} if reference else set(IMAGE_MIMES)
        if image.format not in allowed or image.width * image.height > 40000000:
            raise ValueError("图片格式或尺寸不支持。")
        mime = IMAGE_MIMES[image.format]
        image.verify()
    return mime


def _read_reference(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(MAX_REFERENCE_BYTES + 1)
    _validate_image(data, reference=True)
    return base64.b64encode(data).decode("ascii")


def _prepare_result(value: str) -> tuple[str, str]:
    if len(value) > 4 * math.ceil(MAX_RESULT_BYTES / 3):
        raise ValueError("生成图片超过 32 MB，无法在绘图页面预览。")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("生图服务未返回有效图片。") from exc
    return value, _validate_image(data)


class TrialManager:
    """每个插件实例最多一项绘图与一张参考图，不写入聊天或配置。"""

    def __init__(self, plugin: "MaisArtPlugin") -> None:
        self.plugin = plugin
        self.job: Optional[TrialJob] = None
        self.task: Optional[asyncio.Task] = None
        self.reference_id = ""
        self.reference_base64 = ""
        self.closed = False

    async def upload(self, source: Path) -> Dict[str, Any]:
        data = await asyncio.to_thread(_read_reference, source)
        if self.closed:
            raise RuntimeError("绘图页面已停止。")
        self.reference_id = uuid.uuid4().hex
        self.reference_base64 = data
        return {"success": True, "reference_id": self.reference_id}

    def start(self, payload: str, request_id: str) -> Dict[str, Any]:
        if self.closed:
            return {"success": False, "error": "绘图页面已停止。"}
        if len(request_id) != 32 or any(char not in "0123456789abcdef" for char in request_id):
            return {"success": False, "error": "绘图请求编号无效。"}
        # 请求回执遗失后的同编号重试只返回原任务，避免重复调用付费后端。
        if self.job and self.job.id == request_id:
            return {"success": True, "job": self.job.snapshot()}
        if self.job and self.job.state == "running":
            return {"success": False, "error": "已有一项绘图正在生成。", "job": self.job.snapshot()}
        try:
            if len(payload.encode("utf-8")) > 16384:
                raise ValueError("绘图描述过长。")
            request = TrialRequest.model_validate_json(payload)
            if request.model_id and not any(model.id == request.model_id for model in self.plugin.config.models.items):
                raise ValueError("模型尚未保存，请先保存模型配置。")
            if request.reference_id and request.reference_id != self.reference_id:
                raise ValueError("参考图已失效，请重新上传。")
        except ValidationError:
            return {"success": False, "error": "绘图参数无效，请检查描述、尺寸和图生图强度。"}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        image = self.reference_base64 if request.reference_id else None
        self.job = TrialJob(request_id, request)
        self.task = asyncio.create_task(self._generate(self.job, image), name=f"mais-art:trial:{request_id}")
        return {"success": True, "job": self.job.snapshot()}

    async def _generate(self, job: TrialJob, image: Optional[str]) -> None:
        try:
            # 包含排队、图像下载与结果校验；后台任务不能因页面 RPC 时限而中断。
            async with asyncio.timeout(660):
                result = await self.plugin.api_generate_image(
                    prompt=job.request.prompt, model_id=job.request.model_id,
                    size=job.request.size, negative_prompt=job.request.negative_prompt,
                    input_image_base64=image, strength=job.request.strength if image else None,
                    selfie_mode=job.request.selfie_mode, selfie_style=job.request.selfie_style,
                    use_cache=False,
                )
                if not result.get("success"):
                    job.error = str(result.get("error") or "绘图失败，请检查模型配置。")[:1000]
                    job.state = "failed"
                    return
                job.image_base64, job.mime = await asyncio.to_thread(_prepare_result, result["image_base64"])
                job.model_id = result.get("model_id", "")
                job.size = result.get("size", "")
                job.is_img2img = bool(result.get("is_img2img"))
                job.state = "ready"
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except TimeoutError:
            job.state = "failed"
            job.error = "本次绘图超过等待时限。"
        except (ValueError, OSError):
            logger.exception("绘图结果不是可预览的图片")
            job.state = "failed"
            job.error = "生图服务返回的图片无法预览，请检查图片格式及大小。"
        except Exception:
            logger.exception("绘图后台任务失败")
            job.state = "failed"
            job.error = "绘图失败，请查看插件日志。"
        finally:
            job.finished_at = time.monotonic()

    def status(self, job_id: str = "") -> Dict[str, Any]:
        if job_id and (not self.job or self.job.id != job_id):
            return {"success": False, "error": "绘图任务已失效，请重新绘图。"}
        return {"success": True, "job": self.job.snapshot() if self.job else {"state": "idle"}, "reference_id": self.reference_id}

    def remove_reference(self, reference_id: str) -> Dict[str, Any]:
        if reference_id != self.reference_id:
            return {"success": False, "error": "参考图已更新，请查询状态后再操作。"}
        self.reference_id = ""
        self.reference_base64 = ""
        return {"success": True}

    def chunk(self, job_id: str, index: int) -> Dict[str, Any]:
        if not self.job or self.job.id != job_id or self.job.state != "ready":
            return {"success": False, "error": "绘图结果已失效。"}
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < math.ceil(len(self.job.image_base64) / CHUNK_SIZE):
            return {"success": False, "error": "图片分片编号无效。"}
        offset = index * CHUNK_SIZE
        return {"success": True, "data": self.job.image_base64[offset:offset + CHUNK_SIZE]}

    async def cancel(self, job_id: str) -> Dict[str, Any]:
        if not self.job or self.job.id != job_id:
            return {"success": False, "error": "绘图任务已失效。"}
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            # 任务尚未获调度就被取消时，协程中的 finally 不会执行。
            if self.job.state == "running":
                self.job.state = "cancelled"
                self.job.finished_at = time.monotonic()
        return self.status(job_id)

    async def close(self) -> None:
        self.closed = True
        if self.job:
            await self.cancel(self.job.id)
        self.job = None
        self.task = None
        self.reference_id = ""
        self.reference_base64 = ""
