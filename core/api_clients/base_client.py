"""API客户端基类"""
import asyncio
import base64
import logging
from typing import Dict, Any, Tuple, Optional

from .client_context import ClientContext

logger = logging.getLogger("plugin.mais_art_journal.api")


class NonRetryableError(Exception):
    """不可重试的错误（如内容审核拒绝），直接终止重试循环"""
    pass


# ComfyUI 客户端返回值里的"排队中"标记：命中时上层直接返回、不进入重试循环，
# 从而避免"误把排队当失败"触发重复提交导致队列滚雪球式堆积。
QUEUED_MARK = "[QUEUED]"


class BaseApiClient:
    """API客户端基类

    构造时接收 ClientContext（新接口）或任何具备 `get_config + log_prefix` 的
    duck-typed 对象（兼容 RequestContext / 旧 action_instance）。
    """

    # 子类需要设置的格式名称
    format_name: str = "base"

    def __init__(self, client_ctx):
        # 兼容三类调用方：ClientContext / RequestContext / 旧 action_instance
        if not hasattr(client_ctx, "get_config") or not hasattr(client_ctx, "log_prefix"):
            raise TypeError(
                "BaseApiClient 需要一个具备 get_config 与 log_prefix 的上下文对象"
            )
        self.ctx = client_ctx
        # 保留 self.action 别名，便于 9 个旧 client 渐进改造
        self.action = client_ctx
        self.log_prefix = client_ctx.log_prefix

    def _get_proxy_config(self) -> Optional[Dict[str, Any]]:
        """获取代理配置（ClientContext 走 get_proxy_config，duck-typed 回退读 get_config）"""
        try:
            # 优先走 ClientContext 提供的封装
            if isinstance(self.ctx, ClientContext):
                cfg = self.ctx.get_proxy_config()
                if cfg:
                    logger.info(f"{self.log_prefix} 代理已启用: {cfg.get('http')}")
                return cfg

            # 回退：从 get_config 平铺读取
            proxy_enabled = self.ctx.get_config("proxy.enabled", False)
            if not proxy_enabled:
                return None

            proxy_url = self.ctx.get_config("proxy.url", "http://127.0.0.1:7890")
            timeout = self.ctx.get_config("proxy.timeout", 60)

            proxy_config = {
                "http": proxy_url,
                "https": proxy_url,
                "timeout": timeout,
            }
            logger.info(f"{self.log_prefix} 代理已启用: {proxy_url}")
            return proxy_config
        except Exception as e:
            logger.warning(f"{self.log_prefix} 获取代理配置失败: {e}, 将不使用代理")
            return None

    def _prepare_image_data_uri(self, image_base64: str) -> str:
        """准备图片的data URI格式

        Args:
            image_base64: base64编码的图片数据

        Returns:
            带有正确MIME类型前缀的data URI
        """
        if image_base64.startswith('data:image'):
            return image_base64

        # 检测图片格式
        if image_base64.startswith('/9j/'):
            return f"data:image/jpeg;base64,{image_base64}"
        elif image_base64.startswith('iVBORw'):
            return f"data:image/png;base64,{image_base64}"
        elif image_base64.startswith('UklGR'):
            return f"data:image/webp;base64,{image_base64}"
        elif image_base64.startswith('R0lGOD'):
            return f"data:image/gif;base64,{image_base64}"
        else:
            return f"data:image/jpeg;base64,{image_base64}"

    def _detect_mime_type(self, image_base64: str) -> str:
        """检测图片MIME类型

        Args:
            image_base64: base64编码的图片数据

        Returns:
            MIME类型字符串
        """
        # 移除data URI前缀（如果存在）
        clean_base64 = image_base64
        if ',' in image_base64:
            clean_base64 = image_base64.split(',')[1]

        if clean_base64.startswith('/9j/'):
            return "image/jpeg"
        elif clean_base64.startswith('iVBORw'):
            return "image/png"
        elif clean_base64.startswith('UklGR'):
            return "image/webp"
        elif clean_base64.startswith('R0lGOD'):
            return "image/gif"
        else:
            return "image/jpeg"  # 默认

    def _get_clean_base64(self, image_base64: str) -> str:
        """获取干净的base64数据（移除data URI前缀）

        Args:
            image_base64: 可能包含data URI前缀的base64数据

        Returns:
            纯base64数据
        """
        if ',' in image_base64:
            return image_base64.split(',')[1]
        return image_base64

    async def generate_image(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None,
        max_retries: int = 2
    ) -> Tuple[bool, str]:
        """生成图片的基础方法，带重试逻辑

        Args:
            prompt: 提示词
            model_config: 模型配置
            size: 图片尺寸
            strength: 图生图强度
            input_image_base64: 输入图片的base64编码
            max_retries: 最大重试次数（不包括首次尝试）

        Returns:
            (成功标志, 结果数据或错误信息)
        """
        total_attempts = max_retries + 1  # 首次尝试 + 重试次数
        for attempt in range(total_attempts):
            try:
                if attempt > 0:
                    logger.info(f"{self.log_prefix} API调用重试第 {attempt} 次")
                    await asyncio.sleep(1.0 * attempt)  # 渐进式等待

                logger.debug(f"{self.log_prefix} 开始API调用（尝试 {attempt + 1}/{total_attempts}）")

                # 调用具体实现
                success, result = await asyncio.to_thread(
                    self._make_request,
                    prompt=prompt,
                    model_config=model_config,
                    size=size,
                    strength=strength,
                    input_image_base64=input_image_base64
                )

                if success:
                    if attempt > 0:
                        logger.info(f"{self.log_prefix} API调用重试第 {attempt} 次成功")
                    return True, result

                # 失败处理
                if result.startswith(QUEUED_MARK):
                    # 排队中而非真失败：直接返回，不触发重试（否则会重复提交、堆积队列）
                    logger.warning(f"{self.log_prefix} {result}")
                    return False, result

                if attempt < max_retries:
                    logger.warning(f"{self.log_prefix} 第 {attempt + 1} 次API调用失败: {result}，将重试（剩余 {max_retries - attempt} 次）")
                    continue
                else:
                    if max_retries > 0:
                        logger.error(f"{self.log_prefix} 重试 {max_retries} 次后API调用仍失败: {result}")
                    else:
                        logger.error(f"{self.log_prefix} API调用失败: {result}")
                    return False, result

            except NonRetryableError as e:
                logger.error(f"{self.log_prefix} 不可重试的错误，跳过剩余重试: {e}")
                return False, str(e)

            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"{self.log_prefix} 第 {attempt + 1} 次API调用异常: {e}，将重试（剩余 {max_retries - attempt} 次）")
                    continue
                else:
                    logger.error(f"{self.log_prefix} 重试后API调用仍异常: {e!r}", exc_info=True)
                    return False, f"API调用异常: {str(e)[:100]}"

        return False, "API调用失败"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """具体的请求实现，子类必须实现此方法

        Args:
            prompt: 提示词
            model_config: 模型配置
            size: 图片尺寸
            strength: 图生图强度
            input_image_base64: 输入图片的base64编码

        Returns:
            (成功标志, 结果数据或错误信息)
        """
        raise NotImplementedError("子类必须实现 _make_request 方法")
