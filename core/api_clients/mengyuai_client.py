"""梦羽AI API客户端

梦羽AI图片生成服务 API
支持多种模型，包括Qwen Image Edit版模型用于图生图
"""
import json
import base64
import requests
from typing import Dict, Any, Tuple

from .base_client import BaseApiClient, logger
from ..utils import parse_pixel_size


class MengyuaiClient(BaseApiClient):
    """梦羽AI API客户端"""

    format_name = "mengyuai"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """发送梦羽AI格式的HTTP请求生成图片"""
        try:
            # API配置
            base_url = model_config.get("base_url", "https://sd.exacg.cc").rstrip('/')
            api_key = model_config.get("api_key", "").replace("Bearer ", "")

            if not api_key or api_key in ["YOUR_API_KEY", "xxxxxx"]:
                logger.error(f"{self.log_prefix} (梦羽AI) API密钥未配置")
                return False, "API密钥未配置"

            # 请求头
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            # 获取模型特定的配置参数
            custom_prompt_add = model_config.get("custom_prompt_add", "")
            negative_prompt = model_config.get("negative_prompt_add", "")
            full_prompt = prompt + custom_prompt_add

            # 解析尺寸
            width, height = self._parse_size(size, model_config)

            # 获取模型索引
            model_index = int(model_config.get("model", 0))

            # 构建请求数据
            request_data = {
                "prompt": full_prompt,
                "model_index": model_index,
            }

            # 如果有输入图片，使用Qwen Image Edit模型
            if input_image_base64:
                # 使用图生图模型 (默认 model_index=19 是 Qwen Image Edit版)
                request_data["model_index"] = model_config.get("img2img_model_index", 19)

                # 梦羽AI要求 image_source 是可公网访问的URL
                # 检查是否配置了图片上传服务
                image_upload_url = model_config.get("image_upload_url")

                if image_upload_url:
                    # 上传图片获取URL
                    image_url = self._upload_image(image_upload_url, input_image_base64, api_key)
                    if image_url:
                        request_data["image_source"] = image_url
                    else:
                        logger.warning(f"{self.log_prefix} (梦羽AI) 图片上传失败，降级为文生图模式")
                        request_data["model_index"] = model_index
                else:
                    logger.warning(
                        f"{self.log_prefix} (梦羽AI) 未配置 image_upload_url，"
                        "梦羽AI图生图需要可公网访问的图片URL。将尝试 data URI 但可能不被支持"
                    )
                    image_data_uri = self._prepare_image_data_uri(input_image_base64)
                    request_data["image_source"] = image_data_uri
            else:
                # 文生图模式，添加完整参数
                request_data["negative_prompt"] = negative_prompt
                request_data["width"] = width
                request_data["height"] = height
                request_data["steps"] = model_config.get("num_inference_steps", 20)
                # -1 = 不发送 cfg（部分模型不支持该参数）
                _cfg = model_config.get("guidance_scale", 7.0)
                if _cfg is not None and _cfg != -1:
                    request_data["cfg"] = _cfg
                request_data["seed"] = model_config.get("seed", -1)

            endpoint = f"{base_url}/api/v1/generate_image"

            logger.info(f"{self.log_prefix} (梦羽AI) 发起图片请求: model_index={request_data.get('model_index')}")
            logger.debug(f"{self.log_prefix} (梦羽AI) 完整请求数据: {request_data}")

            # 获取代理配置
            proxy_config = self._get_proxy_config()

            request_kwargs = {
                "url": endpoint,
                "headers": headers,
                "json": request_data,
                "timeout": proxy_config.get('timeout', 120) if proxy_config else 120
            }

            if proxy_config:
                request_kwargs["proxies"] = {
                    "http": proxy_config["http"],
                    "https": proxy_config["https"]
                }

            # 发送请求
            response = requests.post(**request_kwargs)

            if response.status_code != 200:
                error_msg = response.text
                logger.error(f"{self.log_prefix} (梦羽AI) 请求失败: HTTP {response.status_code} - {error_msg}")
                return False, f"请求失败: {error_msg[:100]}"

            # 解析响应
            try:
                result = response.json()
                logger.debug(f"{self.log_prefix} (梦羽AI) 响应JSON: {result}")

                # 检查是否成功 - 梦羽AI可能没有success字段，直接返回图片URL
                # 尝试多种可能的响应格式
                image_url = None
                image_base64 = None

                # 格式1: {"url": "..."} 或 {"image_url": "..."}
                image_url = result.get("url") or result.get("image_url") or result.get("output")

                # 格式2: {"data": {"url": "..."}} 或 {"data": {"image": "..."}}
                if not image_url:
                    data = result.get("data", {})
                    if isinstance(data, dict):
                        image_url = data.get("url") or data.get("image_url") or data.get("output")
                        image_base64 = data.get("image") or data.get("base64")

                # 格式3: {"image": "base64..."} 或 {"base64": "..."}
                if not image_base64:
                    image_base64 = result.get("image") or result.get("base64")

                # 格式4: {"images": ["url1", ...]} 或 {"images": [{"url": "..."}]}
                if not image_url and not image_base64:
                    images = result.get("images", [])
                    if images:
                        if isinstance(images[0], str):
                            image_url = images[0]
                        elif isinstance(images[0], dict):
                            image_url = images[0].get("url") or images[0].get("image_url")

                # 检查是否有错误
                if result.get("error"):
                    error_msg = result.get("error")
                    logger.error(f"{self.log_prefix} (梦羽AI) API返回错误: {error_msg}")
                    return False, f"API错误: {error_msg}"

                if image_base64:
                    logger.info(f"{self.log_prefix} (梦羽AI) 图片生成成功 (base64)")
                    return True, image_base64

                if image_url:
                    # 下载图片
                    logger.info(f"{self.log_prefix} (梦羽AI) 获取到图片URL: {image_url[:100]}...")
                    image_data = self._download_image(image_url, proxy_config)
                    if image_data:
                        logger.info(f"{self.log_prefix} (梦羽AI) 图片生成成功")
                        return True, image_data
                    else:
                        return False, "图片下载失败"

                # 尝试直接返回响应内容（可能是二进制图片）
                content_type = response.headers.get('Content-Type', '')
                if 'image' in content_type:
                    image_base64 = base64.b64encode(response.content).decode('utf-8')
                    logger.info(f"{self.log_prefix} (梦羽AI) 图片生成成功 (binary)")
                    return True, image_base64

                logger.error(f"{self.log_prefix} (梦羽AI) 响应中未找到图片数据，完整响应: {result}")
                return False, "响应中未找到图片数据"

            except json.JSONDecodeError:
                # 可能直接返回的是图片
                content_type = response.headers.get('Content-Type', '')
                if 'image' in content_type:
                    image_base64 = base64.b64encode(response.content).decode('utf-8')
                    logger.info(f"{self.log_prefix} (梦羽AI) 图片生成成功 (直接返回)")
                    return True, image_base64

                logger.error(f"{self.log_prefix} (梦羽AI) 响应解析失败")
                return False, "响应解析失败"

        except requests.RequestException as e:
            logger.error(f"{self.log_prefix} (梦羽AI) 网络请求异常: {e}")
            return False, f"网络请求失败: {str(e)}"

        except Exception as e:
            logger.error(f"{self.log_prefix} (梦羽AI) 请求异常: {e!r}", exc_info=True)
            return False, f"请求失败: {str(e)}"

    def _parse_size(self, size: str, model_config: Dict[str, Any]) -> Tuple[int, int]:
        """解析尺寸字符串（委托给size_utils）"""
        default_width = model_config.get("default_width", 512)
        default_height = model_config.get("default_height", 512)
        return parse_pixel_size(size, default_width, default_height)

    def _download_image(self, url: str, proxy_config: Dict[str, Any] = None) -> str:
        """下载图片并转换为base64

        Args:
            url: 图片URL
            proxy_config: 代理配置

        Returns:
            图片的base64编码，失败返回空字符串
        """
        try:
            request_kwargs = {"url": url, "timeout": 30}

            if proxy_config:
                request_kwargs["proxies"] = {
                    "http": proxy_config["http"],
                    "https": proxy_config["https"]
                }

            response = requests.get(**request_kwargs)

            if response.status_code == 200:
                return base64.b64encode(response.content).decode('utf-8')

        except Exception as e:
            logger.error(f"{self.log_prefix} (梦羽AI) 图片下载失败: {e}")

        return ""

    def _upload_image(self, upload_url: str, image_base64: str, api_key: str) -> str:
        """上传图片获取URL

        Args:
            upload_url: 上传服务URL
            image_base64: 图片的base64编码
            api_key: API密钥

        Returns:
            图片URL，失败返回空字符串
        """
        try:
            # 将base64转为bytes
            image_bytes = base64.b64decode(self._get_clean_base64(image_base64))

            headers = {
                "Authorization": f"Bearer {api_key}"
            }

            files = {
                "file": ("image.png", image_bytes, "image/png")
            }

            response = requests.post(upload_url, headers=headers, files=files, timeout=30)

            if response.status_code == 200:
                result = response.json()
                return result.get("url") or result.get("data", {}).get("url", "")

        except Exception as e:
            logger.error(f"{self.log_prefix} (梦羽AI) 图片上传失败: {e}")

        return ""
