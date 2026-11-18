"""OpenAI Chat Completions 格式API客户端

通过 chat/completions 接口生成图片，适用于支持图片生成的 chat 模型。
多策略图片提取：Markdown图片链接、Data URI、Base64特征、URL。

尺寸字段自动适配：
- 像素格式（如 1024x1024）→ payload["size"] = size （标准 OpenAI）
- Gemini 风格（含 ':' 如 16:9、或以 '-' 开头如 -2K、或 16:9-2K）→
  payload["image_aspect_ratio"] + payload["image_resolution"]
"""
from typing import Any, Dict, Tuple
from urllib.error import HTTPError

import json
import re
import traceback
import urllib.request

from .base_client import BaseApiClient, logger
from .openai_http import handle_http_error, normalize_authorization
from ..utils import pixel_size_to_gemini_aspect


def _apply_size_to_payload(
    payload: Dict[str, Any],
    size: str,
    model_config: Dict[str, Any],
    log_prefix: str = "",
) -> None:
    """按尺寸格式自动选择 OpenAI 风格 size 或 Gemini

    判断依据：
    - 像素格式（如 1024x1024）→ payload["size"]
    - 含 ':' 或以 '-' 开头 → 走 Gemini 风格
    """
    if not size:
        return

    s = size.strip()
    if ":" not in s and not s.startswith("-"):
        payload["size"] = s
        return

    # Gemini
    llm_orig = (model_config.get("_llm_original_size") or "").strip()

    if s.startswith("-"):
        # 仅分辨率，如 "-2K"
        payload["image_resolution"] = s[1:].strip().upper()
        if llm_orig:
            payload["image_aspect_ratio"] = pixel_size_to_gemini_aspect(llm_orig, log_prefix) or "1:1"
        else:
            payload["image_aspect_ratio"] = "1:1"
    elif "-" in s and ":" in s:
        # 宽高比+分辨率，如 "16:9-2K"
        parts = s.split("-", 1)
        payload["image_aspect_ratio"] = parts[0].strip()
        payload["image_resolution"] = parts[1].strip().upper()
    else:
        # 仅宽高比，如 "16:9"
        payload["image_aspect_ratio"] = s


class OpenAIChatClient(BaseApiClient):
    """OpenAI Chat Completions 格式API客户端

    通过 /chat/completions 端点请求图片生成，
    从模型的文本响应中提取图片数据。
    """

    format_name = "openai-chat"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """发送 Chat Completions 格式的HTTP请求生成图片"""
        base_url = model_config.get("base_url", "")
        api_key = normalize_authorization(model_config.get("api_key", ""))
        model = model_config.get("model", "")

        endpoint = f"{base_url.rstrip('/')}/chat/completions"

        # 获取模型特定的配置参数
        custom_prompt_add = model_config.get("custom_prompt_add", "")
        negative_prompt_add = model_config.get("negative_prompt_add", "")
        full_prompt = prompt + custom_prompt_add

        # 如果有负面提示词，追加到提示中
        if negative_prompt_add:
            full_prompt += f"\n\nNegative prompt (avoid these): {negative_prompt_add}"

        # 构建 chat messages
        messages = []

        # 系统消息：指导模型生成图片
        system_content = (
            "You are an image generation assistant. Generate an image based on the user's description. "
            f"Target image size: {size}."
        )
        messages.append({"role": "system", "content": system_content})

        # 用户消息
        user_content_parts = []

        # 如果有输入图片（图生图场景），添加图片
        if input_image_base64:
            image_data_uri = self._prepare_image_data_uri(input_image_base64)
            user_content_parts.append({
                "type": "image_url",
                "image_url": {"url": image_data_uri}
            })
            strength_text = f" (modification strength: {strength})" if strength else ""
            user_content_parts.append({
                "type": "text",
                "text": f"Please modify this image based on the following description{strength_text}: {full_prompt}"
            })
            messages.append({"role": "user", "content": user_content_parts})
        else:
            messages.append({"role": "user", "content": f"Please generate an image: {full_prompt}"})

        # 构建请求体
        payload_dict = {
            "model": model,
            "messages": messages,
        }

        # 添加可选的生成参数
        seed = model_config.get("seed", -1)
        if seed is not None and seed != -1:
            payload_dict["seed"] = seed

        # 尺寸字段：按格式自动选择 openai 风格 size 还是 Gemini 风格 image_aspect_ratio + image_resolution
        # （Gemini 风格用于通过 chat/completions 接口转发的 Gemini 代理服务）
        if size:
            _apply_size_to_payload(payload_dict, size, model_config, self.log_prefix)

        data = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"{api_key}",
        }

        # 详细调试信息
        verbose_debug = False
        try:
            verbose_debug = self.ctx.get_config("basic.enable_verbose_debug", False)
        except Exception:
            pass

        if verbose_debug:
            safe_payload = payload_dict.copy()
            # 清理敏感数据
            if "messages" in safe_payload:
                safe_msgs = []
                for msg in safe_payload["messages"]:
                    if isinstance(msg.get("content"), list):
                        safe_parts = []
                        for part in msg["content"]:
                            if part.get("type") == "image_url":
                                safe_parts.append({"type": "image_url", "image_url": {"url": "[BASE64_DATA...]"}})
                            else:
                                safe_parts.append(part)
                        safe_msgs.append({"role": msg["role"], "content": safe_parts})
                    else:
                        safe_msgs.append(msg)
                safe_payload["messages"] = safe_msgs
            safe_headers = headers.copy()
            if "Authorization" in safe_headers:
                auth_value = safe_headers["Authorization"]
                if auth_value.startswith("Bearer "):
                    safe_headers["Authorization"] = "Bearer ***"
                else:
                    safe_headers["Authorization"] = "***"
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 详细调试 - 请求端点: {endpoint}")
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 详细调试 - 请求头: {safe_headers}")
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 详细调试 - 请求体: {json.dumps(safe_payload, ensure_ascii=False, indent=2)}")

        logger.info(f"{self.log_prefix} (OpenAI-Chat) 发起 chat/completions 请求: {model}, Prompt: {full_prompt[:30]}... To: {endpoint}")

        # 获取代理配置
        proxy_config = self._get_proxy_config()

        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

        try:
            # 构建 opener（局部使用，不污染全局）
            if proxy_config:
                proxy_handler = urllib.request.ProxyHandler({
                    'http': proxy_config['http'],
                    'https': proxy_config['https']
                })
                opener = urllib.request.build_opener(proxy_handler)
                timeout = proxy_config.get('timeout', 600)
            else:
                opener = urllib.request.build_opener()
                timeout = 600

            with opener.open(req, timeout=timeout) as response:
                response_status = response.status
                response_body_bytes = response.read()
                response_body_str = response_body_bytes.decode("utf-8")

                logger.info(f"{self.log_prefix} (OpenAI-Chat) 响应状态: {response_status}")

                if verbose_debug:
                    # 清理长 base64 数据用于日志
                    cleaned = self._clean_log_content(response_body_str)
                    logger.info(f"{self.log_prefix} (OpenAI-Chat) 详细调试 - 响应体: {cleaned[:500]}")

                if 200 <= response_status < 300:
                    response_data = json.loads(response_body_str)
                    return self._extract_image_from_response(response_data)
                else:
                    logger.error(f"{self.log_prefix} (OpenAI-Chat) API请求失败. 状态: {response_status}. 正文: {response_body_str[:300]}...")
                    return False, f"Chat API请求失败(状态码 {response_status})"

        except HTTPError as e:
            return handle_http_error(
                e, authorization=api_key, log_prefix=self.log_prefix,
                client_name="OpenAI-Chat", error_label="Chat API请求失败",
            )
        except Exception as e:
            logger.error(f"{self.log_prefix} (OpenAI-Chat) 请求异常: {e!r}", exc_info=True)
            traceback.print_exc()
            return False, f"Chat API请求异常: {str(e)[:100]}"

    def _extract_image_from_response(self, response_data: dict) -> Tuple[bool, str]:
        """从 chat/completions 响应中提取图片数据

        多策略提取：
        1. images 字段（Gemini 3.1 Flash Image 等模型）
        2. Markdown 图片链接: ![...](url)
        3. Data URI: data:image/...;base64,...
        4. Base64 特征检测
        5. 普通 URL
        """
        # 策略0：优先检查 images 字段（Gemini 3.1 Flash Image 等模型）
        try:
            choices = response_data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                images = message.get("images", [])
                
                if images and isinstance(images, list) and len(images) > 0:
                    # 提取第一个图片
                    image_data = images[0]
                    if isinstance(image_data, dict):
                        # 检查 image_url 格式
                        if "image_url" in image_data and isinstance(image_data["image_url"], dict):
                            url = image_data["image_url"].get("url", "")
                            if url:
                                logger.info(f"{self.log_prefix} (OpenAI-Chat) 从 images 字段提取到图片URL: {url[:70]}...")
                                return True, url
                        # 检查直接包含 base64 数据
                        elif "data" in image_data:
                            b64_data = image_data["data"]
                            logger.info(f"{self.log_prefix} (OpenAI-Chat) 从 images 字段提取到 Base64 数据，长度: {len(b64_data)}")
                            return True, b64_data
        except (IndexError, KeyError, TypeError, AttributeError) as e:
            logger.debug(f"{self.log_prefix} (OpenAI-Chat) 解析 images 字段失败: {e}")

        # 提取 assistant 回复内容
        content = ""
        try:
            choices = response_data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
        except (IndexError, KeyError, TypeError):
            pass

        if not content:
            logger.error(f"{self.log_prefix} (OpenAI-Chat) 响应中无 content 字段")
            # 记录完整的响应结构用于调试
            logger.debug(f"{self.log_prefix} (OpenAI-Chat) 完整响应结构: {json.dumps(response_data, ensure_ascii=False)[:500]}...")
            return False, "Chat API响应中无内容"

        logger.debug(f"{self.log_prefix} (OpenAI-Chat) 提取图片，内容长度: {len(content)}")

        # 策略1：Markdown 图片链接 ![alt](url)
        md_pattern = r'!\[.*?\]\((https?://[^\s\)]+)\)'
        md_matches = re.findall(md_pattern, content)
        if md_matches:
            image_url = md_matches[0]
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 从 Markdown 提取到图片URL: {image_url[:70]}...")
            return True, image_url

        # 策略2：Data URI (data:image/xxx;base64,...)
        data_uri_pattern = r'data:image/[a-zA-Z]+;base64,([A-Za-z0-9+/=]+)'
        data_uri_matches = re.findall(data_uri_pattern, content)
        if data_uri_matches:
            b64_data = data_uri_matches[0]
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 从 Data URI 提取到 Base64 数据，长度: {len(b64_data)}")
            return True, b64_data

        # 策略3：Base64 特征检测（连续长 base64 字符串）
        b64_pattern = r'(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{200,}={0,2})(?![A-Za-z0-9+/])'
        b64_matches = re.findall(b64_pattern, content)
        if b64_matches:
            # 取最长的匹配
            longest = max(b64_matches, key=len)
            # 验证是否是有效的 base64 图片数据
            if longest.startswith(('/9j/', 'iVBORw', 'UklGR', 'R0lGOD')) or len(longest) > 1000:
                logger.info(f"{self.log_prefix} (OpenAI-Chat) 检测到 Base64 图片数据，长度: {len(longest)}")
                return True, longest

        # 策略4：普通 URL（http/https 图片链接）
        url_pattern = r'(https?://[^\s<>"\']+\.(?:png|jpg|jpeg|gif|webp|bmp)(?:\?[^\s<>"\']*)?)'
        url_matches = re.findall(url_pattern, content, re.IGNORECASE)
        if url_matches:
            image_url = url_matches[0]
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 从内容提取到图片URL: {image_url[:70]}...")
            return True, image_url

        # 策略5：任意 URL（可能是不带扩展名的图片链接）
        any_url_pattern = r'(https?://[^\s<>"\']+)'
        any_url_matches = re.findall(any_url_pattern, content)
        if any_url_matches:
            # 只取第一个 URL，可能是图片
            image_url = any_url_matches[0]
            logger.info(f"{self.log_prefix} (OpenAI-Chat) 从内容提取到候选URL: {image_url[:70]}...")
            return True, image_url

        logger.error(f"{self.log_prefix} (OpenAI-Chat) 无法从响应中提取图片。响应预览: {json.dumps(response_data, ensure_ascii=False)[:500]}...")
        return False, "无法从 Chat API 响应中提取图片数据"

    def _clean_log_content(self, content: str) -> str:
        """清理日志中的长 base64 数据"""
        # 替换长 base64 字符串
        cleaned = re.sub(
            r'[A-Za-z0-9+/]{200,}={0,2}',
            '[BASE64_DATA...]',
            content
        )
        return cleaned
