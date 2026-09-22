"""豆包（火山引擎）API客户端"""
from typing import Dict, Any, Tuple

from .base_client import BaseApiClient, NonRetryableError, logger


class DoubaoClient(BaseApiClient):
    """豆包（火山引擎）API客户端"""

    format_name = "doubao"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """发送豆包格式的HTTP请求生成图片"""
        try:
            # 尝试导入豆包SDK
            try:
                from volcenginesdkarkruntime import Ark
            except ImportError:
                logger.error(f"{self.log_prefix} (Doubao) 缺少volcenginesdkarkruntime库，请安装: pip install 'volcengine-python-sdk[ark]'")
                return False, "缺少豆包SDK，请安装volcengine-python-sdk[ark]"

            # 获取代理配置
            proxy_config = self._get_proxy_config()

            # 初始化客户端
            api_key = model_config.get("api_key", "").replace("Bearer ", "")
            client_kwargs = {
                "base_url": model_config.get("base_url"),
                "api_key": api_key,
            }

            # 如果启用了代理，配置代理
            if proxy_config:
                proxy_url = proxy_config["http"]
                client_kwargs["proxies"] = {
                    "http://": proxy_url,
                    "https://": proxy_url
                }
                client_kwargs["timeout"] = proxy_config["timeout"]

            client = Ark(**client_kwargs)

            # 获取模型特定的配置参数
            custom_prompt_add = model_config.get("custom_prompt_add", "")
            prompt_add = prompt + custom_prompt_add

            # 构建请求参数
            request_params = {
                "model": model_config.get("model"),
                "prompt": prompt_add,
                "size": size,
                "response_format": "url",
                "watermark": model_config.get("watermark", True)
            }

            # 添加可选参数
            seed = model_config.get("seed")
            if seed is not None and seed != -1:
                request_params["seed"] = seed

            # -1 = 该模型不支持 guidance_scale，不发送（与 openai_client 保持一致）
            guidance_scale = model_config.get("guidance_scale")
            if guidance_scale is not None and guidance_scale != -1:
                request_params["guidance_scale"] = guidance_scale

            # 如果有输入图片，需要特殊处理
            if input_image_base64:
                image_data_uri = self._prepare_image_data_uri(input_image_base64)
                request_params["image"] = image_data_uri
                logger.info(f"{self.log_prefix} (Doubao) 使用图生图模式，图片格式: {image_data_uri[:50]}...")

            logger.info(f"{self.log_prefix} (Doubao) 发起图片请求: {model_config.get('model')}, Size: {size}")

            response = client.images.generate(**request_params)

            if response.data and len(response.data) > 0:
                image_url = response.data[0].url
                logger.info(f"{self.log_prefix} (Doubao) 图片生成成功: {image_url[:70]}...")
                return True, image_url
            else:
                logger.error(f"{self.log_prefix} (Doubao) 响应中没有图片数据")
                return False, "豆包API响应成功但未返回图片"

        except Exception as e:
            error_str = str(e)
            # 内容审核拒绝等永久性错误，不应重试
            non_retryable_codes = [
                "OutputImageSensitiveContentDetected",
                "InputImageSensitiveContentDetected",
                "ImageSensitiveContentDetected",
                "ContentFilterBlocked",
            ]
            for code in non_retryable_codes:
                if code in error_str:
                    raise NonRetryableError(f"豆包内容审核拒绝: {error_str[:100]}")
            logger.error(f"{self.log_prefix} (Doubao) 请求异常: {e!r}", exc_info=True)
            return False, f"豆包API请求失败: {error_str[:100]}"
