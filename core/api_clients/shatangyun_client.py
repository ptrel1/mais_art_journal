"""砂糖云API客户端 (NovelAI)

砂糖云是一个NovelAI图片生成代理服务
API格式：GET请求，参数通过URL传递
示例：https://std.loliyc.com/generate?tag=prompt&token=xxx&model=nai-diffusion-4-5-full&size=832x1216&steps=23&scale=5&cfg=0&sampler=k_euler_ancestral&nocache=0&noise_schedule=karras
"""
import base64
import requests
from typing import Dict, Any, Tuple
from urllib.parse import urlencode

from .base_client import BaseApiClient, logger


class ShatangyunClient(BaseApiClient):
    """砂糖云API客户端"""

    format_name = "shatangyun"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """发送砂糖云格式的HTTP请求生成图片"""
        try:
            # API配置
            base_url = model_config.get("base_url", "https://std.loliyc.com").rstrip('/')
            token = model_config.get("api_key", "").replace("Bearer ", "")
            model = model_config.get("model", "nai-diffusion-4-5-full")

            # 获取模型特定的配置参数
            custom_prompt_add = model_config.get("custom_prompt_add", "")
            full_prompt = prompt + custom_prompt_add

            # 尺寸参数：直接使用传入的像素格式（如 832x1216）
            size_param = size if size else model_config.get("default_size", "832x1216")

            # 构建请求参数
            params = {
                "tag": full_prompt,
                "token": token,
                "model": model,
                "size": size_param,
                "steps": model_config.get("num_inference_steps", 23),
                "cfg": model_config.get("cfg", 0),
                "sampler": model_config.get("sampler", "k_euler_ancestral"),
                "nocache": model_config.get("nocache", 0),
                "noise_schedule": model_config.get("noise_schedule", "karras"),
            }

            # -1 = 不发送 scale（部分模型不支持该参数）
            _scale = model_config.get("guidance_scale", 5)
            if _scale is not None and _scale != -1:
                params["scale"] = _scale

            # 添加artist参数
            artist = model_config.get("artist", "")
            if artist:
                params["artist"] = artist

            # 添加负面提示词
            negative_prompt = model_config.get("negative_prompt_add", "")
            if negative_prompt:
                params["negative"] = negative_prompt

            # 添加种子
            seed = model_config.get("seed")
            if seed is not None and seed != -1:
                params["seed"] = seed

            # 构建URL
            endpoint = f"{base_url}/generate"
            url = f"{endpoint}?{urlencode(params)}"

            logger.info(f"{self.log_prefix} (砂糖云) 发起图片请求: {model}, Size: {size_param}")
            logger.debug(f"{self.log_prefix} (砂糖云) URL: {url[:100]}...")

            # 获取代理配置
            proxy_config = self._get_proxy_config()

            request_kwargs = {
                "url": url,
                "timeout": proxy_config.get('timeout', 120) if proxy_config else 120
            }

            if proxy_config:
                request_kwargs["proxies"] = {
                    "http": proxy_config["http"],
                    "https": proxy_config["https"]
                }

            # 发送GET请求获取图片
            response = requests.get(**request_kwargs)

            if response.status_code != 200:
                logger.error(f"{self.log_prefix} (砂糖云) 请求失败: HTTP {response.status_code}")
                return False, f"请求失败: HTTP {response.status_code}"

            # 检查返回的内容类型
            content_type = response.headers.get('Content-Type', '')
            if 'image' in content_type:
                # 直接返回图片的base64编码
                image_base64 = base64.b64encode(response.content).decode('utf-8')
                logger.info(f"{self.log_prefix} (砂糖云) 图片生成成功，大小: {len(response.content)} bytes")
                return True, image_base64
            else:
                # 可能返回了错误信息
                error_text = response.text[:200]
                logger.error(f"{self.log_prefix} (砂糖云) 未返回图片数据: {error_text}")
                return False, f"未返回图片数据: {error_text}"

        except requests.RequestException as e:
            logger.error(f"{self.log_prefix} (砂糖云) 网络请求异常: {e}")
            return False, f"网络请求失败: {str(e)}"

        except Exception as e:
            logger.error(f"{self.log_prefix} (砂糖云) 请求异常: {e!r}", exc_info=True)
            return False, f"请求失败: {str(e)}"
