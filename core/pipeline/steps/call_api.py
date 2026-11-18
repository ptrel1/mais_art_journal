"""Step: 调用 API 客户端生图

- 缓存命中时跳过
- debug 模式发送"开始生图"提示
- client_factory(api_format) 获取客户端实例
- 失败 → fail
"""
from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Optional

from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.mais_art_journal.step.call_api")


class CallApi(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if req.cache_hit or not req.resolved_model_config:
            return None

        plugin = ctx.plugin

        if req.debug_info and req.stream_id and req.source in ("tool", "cmd_style", "cmd_natural"):
            mode_text = "图生图" if req.is_img2img else "文生图"
            model_name = req.resolved_model_config.get("model", "default-model")
            try:
                await plugin.ctx.send.text(
                    f"收到！正在为您使用 {req.model_id or '默认'} 模型进行{mode_text}，描述: "
                    f"'{req.description[:80]}'，请稍候...（模型: {model_name}, 尺寸: {req.final_size}）",
                    req.stream_id,
                )
            except Exception as e:
                logger.warning(f"{req.log_prefix} 发送调试提示失败: {e}")

        max_retries = plugin.config.basic.max_retries

        try:
            client = ctx.client_factory(req.api_format)
            req.generation_started = True
            ok, result = await client.generate_image(
                prompt=req.description,
                model_config=req.resolved_model_config,
                size=req.final_size,
                strength=req.strength,
                input_image_base64=req.input_image_base64,
                max_retries=max_retries,
            )
        except Exception as e:
            logger.error(f"{req.log_prefix} 异步请求执行失败: {e!r}", exc_info=True)
            traceback.print_exc()
            mode_text = "图生图" if req.is_img2img else "文生图"
            return StepResult.fail(
                error=f"API 异常: {e!r}",
                user_message=f"哎呀，{mode_text}时遇到问题：{str(e)[:100]}",
            )

        if not ok:
            mode_text = "图生图" if req.is_img2img else "文生图"
            return StepResult.fail(
                error=f"{mode_text}失败: {result}",
                user_message=f"哎呀，{mode_text}时遇到问题：{result}",
            )

        req.raw_api_result = result
        return None
