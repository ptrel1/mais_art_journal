"""OpenAI格式API客户端

支持：OpenAI官方、硅基流动、NewAPI、火山方舟等兼容OpenAI格式的服务
"""
from typing import Any, Dict, Tuple
from urllib.error import HTTPError

import json
import traceback
import urllib.request

from .base_client import BaseApiClient, logger
from .openai_http import handle_http_error, normalize_authorization


class OpenAIClient(BaseApiClient):
    """OpenAI格式API客户端"""

    format_name = "openai"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """发送OpenAI格式的HTTP请求生成图片"""
        base_url = model_config.get("base_url", "")
        generate_api_key = normalize_authorization(model_config.get("api_key", ""))
        model = model_config.get("model", "")

        # 智能拼接端点：
        # - base_url 已含 /images/generations 或 /images/edits → 直接用
        # - 否则在末尾拼 /images/generations
        # 这样既支持标准 OpenAI（base_url=https://api.openai.com/v1），
        # 也支持智谱 GLM（base_url=https://open.bigmodel.cn/api/paas/v4）、
        # 模力方舟（base_url=https://ai.gitee.com/v1）等非 /v1 结尾的平台。
        base = base_url.rstrip("/")
        if base.endswith(("/images/generations", "/images/edits")):
            endpoint = base
        else:
            endpoint = f"{base}/images/generations"

        # 获取模型特定的配置参数
        custom_prompt_add = model_config.get("custom_prompt_add", "")
        negative_prompt_add = model_config.get("negative_prompt_add", "")
        seed = model_config.get("seed", -1)
        guidance_scale = model_config.get("guidance_scale", 7.5)
        watermark = model_config.get("watermark", True)
        num_inference_steps = model_config.get("num_inference_steps", 20)
        prompt_add = prompt + custom_prompt_add
        negative_prompt = negative_prompt_add

        # 构建基本请求参数
        payload_dict = {
            "model": model,
            "prompt": prompt_add,
            "size": size,
            "n": 1,
        }

        # 添加可选参数
        if negative_prompt:
            payload_dict["negative_prompt"] = negative_prompt
        if seed is not None and seed != -1:
            payload_dict["seed"] = seed

        # 如果有输入图片，添加图生图参数
        if input_image_base64:
            image_data_uri = self._prepare_image_data_uri(input_image_base64)
            payload_dict["image"] = image_data_uri
            if strength is not None:
                payload_dict["strength"] = strength

        # 根据不同API添加特定参数
        if "ark.cn-beijing.volces.com" in base_url:  # 豆包火山方舟
            payload_dict["watermark"] = watermark
        else:  # 默认魔搭等其他
            # 兼容性优化：当guidance_scale或num_inference_steps为None或-1时不发送对应参数
            # 某些模型（如Gemini 3.1 Flash Image）不支持这些参数
            if guidance_scale is not None and guidance_scale != -1:
                payload_dict["guidance_scale"] = guidance_scale
            if num_inference_steps is not None and num_inference_steps != -1:
                payload_dict["num_inference_steps"] = num_inference_steps

        # 平台兼容性处理
        is_siliconflow = "siliconflow" in base_url.lower() or "api.siliconflow.cn" in base_url.lower()
        is_openai_official = "api.openai.com" in base_url.lower()
        is_grok = "api.x.ai" in base_url.lower()

        if is_siliconflow:
            # 硅基流动：使用 image_size 代替 size，batch_size 代替 n
            if "size" in payload_dict:
                payload_dict["image_size"] = payload_dict.pop("size")
            if "n" in payload_dict:
                payload_dict["batch_size"] = payload_dict.pop("n")

            # 根据模型选择正确的参数
            model_lower = model.lower()
            if "qwen" in model_lower:
                # Qwen-Image 系列使用 cfg 而非 guidance_scale
                if "guidance_scale" in payload_dict:
                    payload_dict["cfg"] = payload_dict.pop("guidance_scale")
                # Qwen-Image-Edit 不支持 image_size
                if "image-edit" in model_lower and "image_size" in payload_dict:
                    del payload_dict["image_size"]
            else:
                # Kolors 等其他模型使用 guidance_scale
                pass

            logger.debug(f"{self.log_prefix} (OpenAI) 检测到硅基流动平台，使用 image_size/batch_size 参数")

        elif is_openai_official:
            # OpenAI官方：只保留标准参数
            standard_params = ["model", "prompt", "size", "n", "quality", "style", "response_format"]
            if input_image_base64:
                standard_params.extend(["image", "strength"])
            payload_dict = {k: v for k, v in payload_dict.items() if k in standard_params}
            logger.debug(f"{self.log_prefix} (OpenAI) 检测到OpenAI官方平台，仅使用标准参数")

        elif is_grok:
            # Grok：只保留 model, prompt, n, response_format
            supported = ["model", "prompt", "n", "response_format"]
            payload_dict = {k: v for k, v in payload_dict.items() if k in supported}
            logger.debug(f"{self.log_prefix} (OpenAI) 检测到Grok平台，仅保留支持的参数")

        data = json.dumps(payload_dict).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"{generate_api_key}",
        }

        # 详细调试信息
        verbose_debug = self.ctx.get_config("basic.enable_verbose_debug", False)
        if verbose_debug:
            # 记录完整的请求payload（隐藏敏感信息）
            safe_payload = payload_dict.copy()
            # 不记录图片base64数据，因为太长
            if "image" in safe_payload:
                safe_payload["image"] = "[BASE64_DATA...]"
            # 创建安全的请求头副本，隐藏Authorization值
            safe_headers = headers.copy()
            if "Authorization" in safe_headers:
                auth_value = safe_headers["Authorization"]
                # 如果包含Bearer，保留Bearer前缀，隐藏其余部分
                if auth_value.startswith("Bearer "):
                    safe_headers["Authorization"] = "Bearer ***"
                else:
                    safe_headers["Authorization"] = "***"
            logger.info(f"{self.log_prefix} (OpenAI) 详细调试 - 请求端点: {endpoint}")
            logger.info(f"{self.log_prefix} (OpenAI) 详细调试 - 请求头: {safe_headers}")
            logger.info(f"{self.log_prefix} (OpenAI) 详细调试 - 请求体: {json.dumps(safe_payload, ensure_ascii=False, indent=2)}")

        logger.info(f"{self.log_prefix} (OpenAI) 发起图片请求: {model}, Prompt: {prompt_add[:30]}... To: {endpoint}")

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
                # 清理响应体中的base64图片数据
                cleaned_response = self._clean_response_body(response_body_str)
                logger.info(f"{self.log_prefix} (OpenAI) 响应: {response_status}. Preview: {cleaned_response[:150]}...")

                # 详细调试信息
                if verbose_debug:
                    logger.info(f"{self.log_prefix} (OpenAI) 详细调试 - 完整响应体: {cleaned_response}")

                if 200 <= response_status < 300:
                    response_data = json.loads(response_body_str)
                    b64_data = None
                    image_url = None

                    # 优先检查Base64数据
                    if (
                        isinstance(response_data.get("data"), list)
                        and response_data["data"]
                        and isinstance(response_data["data"][0], dict)
                        and "b64_json" in response_data["data"][0]
                    ):
                        b64_data = response_data["data"][0]["b64_json"]
                        logger.info(f"{self.log_prefix} (OpenAI) 获取到Base64图片数据，长度: {len(b64_data)}")
                        return True, b64_data
                    elif (
                        isinstance(response_data.get("data"), list)
                        and response_data["data"]
                        and isinstance(response_data["data"][0], dict)
                    ):
                        image_url = response_data["data"][0].get("url")
                    elif (  # 魔搭社区返回的 json
                        isinstance(response_data.get("images"), list)
                        and response_data["images"]
                        and isinstance(response_data["images"][0], dict)
                    ):
                        image_url = response_data["images"][0].get("url")
                    elif response_data.get("url"):
                        image_url = response_data.get("url")

                    if image_url:
                        logger.info(f"{self.log_prefix} (OpenAI) 图片生成成功，URL: {image_url[:70]}...")
                        return True, image_url
                    else:
                        logger.error(f"{self.log_prefix} (OpenAI) API成功但无图片URL. 响应预览: {cleaned_response[:300]}...")
                        return False, "图片生成API响应成功但未找到图片URL"
                else:
                    logger.error(f"{self.log_prefix} (OpenAI) API请求失败. 状态: {response.status}. 正文: {cleaned_response[:300]}...")
                    return False, f"图片API请求失败(状态码 {response.status})"
        except HTTPError as e:
            return handle_http_error(
                e, authorization=generate_api_key, log_prefix=self.log_prefix,
                client_name="OpenAI", error_label="图片API请求失败",
            )
        except Exception as e:
            logger.error(f"{self.log_prefix} (OpenAI) 图片生成时意外错误: {e!r}", exc_info=True)
            traceback.print_exc()
            return False, f"图片生成HTTP请求时发生意外错误: {str(e)[:100]}"

    def _clean_response_body(self, response_body: str) -> str:
        """清理响应体中的base64图片数据，避免日志打印完整的base64字符串
        
        Args:
            response_body: 原始响应体字符串
            
        Returns:
            清理后的响应体，base64数据被替换为占位符
        """
        try:
            # 如果响应体是JSON，尝试解析并替换b64_json字段
            import json
            data = json.loads(response_body)
            if isinstance(data, dict):
                # 检查是否有b64_json字段
                if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                    for item in data["data"]:
                        if isinstance(item, dict) and "b64_json" in item:
                            item["b64_json"] = "[BASE64_DATA...]"
                # 检查是否有images字段（魔搭格式）
                if "images" in data and isinstance(data["images"], list) and len(data["images"]) > 0:
                    for i, img in enumerate(data["images"]):
                        if isinstance(img, dict) and "url" in img:
                            # URL可以保留
                            pass
                # 重新序列化为字符串
                return json.dumps(data, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            # 如果不是JSON，检查是否是纯base64图片数据
            # 常见的base64图片前缀
            base64_prefixes = ['/9j/', 'iVBORw', 'UklGR', 'R0lGOD']
            if any(response_body.startswith(prefix) for prefix in base64_prefixes):
                return "[BASE64_IMAGE_DATA...]"
            # 如果包含很长的base64字符串（长度>500），截断
            if len(response_body) > 500 and all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' for c in response_body[:100]):
                return f"[BASE64_DATA_LEN:{len(response_body)}]"
        # 其他情况返回原样
        return response_body
