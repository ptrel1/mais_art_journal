"""Step: 图生图回退检测

- 有 input_image_base64 但模型不支持图生图 → 清空 input_image_base64 + strength
- silent_img2img_fallback=True 时静默（自拍参考图回退）
- 否则发用户消息
- 写回 req.is_img2img
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.mais_art_journal.step.img2img_fallback")


class DetectImg2ImgFallback(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if req.input_image_base64 and req.resolved_model_config:
            if not req.resolved_model_config.get("support_img2img", True):
                logger.warning(f"{req.log_prefix} 模型 {req.model_id} 不支持图生图，转为文生图")
                if not req.silent_img2img_fallback and req.stream_id:
                    try:
                        await ctx.plugin.ctx.send.text(
                            f"当前模型 {req.model_id} 不支持图生图功能，将为您生成新图片",
                            req.stream_id,
                        )
                    except Exception as e:
                        logger.warning(f"{req.log_prefix} 发送回退提示失败: {e}")
                req.input_image_base64 = None
                req.strength = None
                logger.info(
                    f"{req.log_prefix} 模型 {req.model_id} 不支持图生图，已降级为文生图"
                    f"（提示：可在「基础配置.默认图生图模型」配置支持图生图的模型）"
                )

        req.is_img2img = req.input_image_base64 is not None
        return None
