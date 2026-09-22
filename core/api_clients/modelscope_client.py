"""魔搭社区API客户端"""
import json
import time
import base64
import requests
from typing import Dict, Any, Tuple

from .base_client import BaseApiClient, logger


class ModelscopeClient(BaseApiClient):
    """魔搭社区API客户端"""

    format_name = "modelscope"

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """发送魔搭格式的HTTP请求生成图片"""
        try:
            # API配置
            api_key = model_config.get("api_key", "").replace("Bearer ", "")
            model_name = model_config.get("model", "MusePublic/489_ckpt_FLUX_1")
            base_url = model_config.get("base_url", "https://api-inference.modelscope.cn").rstrip('/')

            # 验证API密钥
            if not api_key or api_key in ["xxxxxxxxxxxxxx", "YOUR_API_KEY_HERE"]:
                logger.error(f"{self.log_prefix} (魔搭) API密钥未配置或无效")
                return False, "魔搭API密钥未配置，请在配置文件中设置正确的API密钥"

            # 请求头
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-ModelScope-Async-Mode": "true"
            }

            logger.info(f"{self.log_prefix} (魔搭) 使用模型: {model_name}, API地址: {base_url}")

            # 添加额外的提示词前缀
            custom_prompt_add = model_config.get("custom_prompt_add", "")
            full_prompt = prompt + custom_prompt_add

            # 获取其他配置参数
            guidance = model_config.get("guidance_scale", 3.5)
            steps = model_config.get("num_inference_steps", 30)
            negative_prompt = model_config.get("negative_prompt_add", "")
            seed = model_config.get("seed", 42)

            # 根据是否有输入图片，构建不同的请求参数
            if input_image_base64:
                image_data_uri = self._prepare_image_data_uri(input_image_base64)
                request_data = {
                    "model": model_name,
                    "prompt": full_prompt,
                    "image_url": [image_data_uri]
                }
                logger.info(f"{self.log_prefix} (魔搭) 使用图生图模式")
            else:
                request_data = {
                    "model": model_name,
                    "prompt": full_prompt
                }
                if negative_prompt:
                    request_data["negative_prompt"] = negative_prompt
                if size:
                    request_data["size"] = size
                request_data["seed"] = seed
                request_data["steps"] = steps
                # -1 = 不发送 guidance（部分模型不支持该参数）
                if guidance is not None and guidance != -1:
                    request_data["guidance"] = guidance
                logger.info(f"{self.log_prefix} (魔搭) 使用文生图模式")

            logger.info(f"{self.log_prefix} (魔搭) 发起异步图片生成请求，模型: {model_name}")

            # 获取代理配置
            proxy_config = self._get_proxy_config()
            endpoint = f"{base_url.rstrip('/')}/images/generations"

            request_kwargs = {
                "url": endpoint,
                "headers": headers,
                "data": json.dumps(request_data, ensure_ascii=False).encode('utf-8'),
                "timeout": proxy_config.get('timeout', 30) if proxy_config else 30
            }

            if proxy_config:
                request_kwargs["proxies"] = {
                    "http": proxy_config["http"],
                    "https": proxy_config["https"]
                }

            # 发送异步请求
            response = requests.post(**request_kwargs)

            if response.status_code != 200:
                error_msg = response.text
                logger.error(f"{self.log_prefix} (魔搭) 请求失败: HTTP {response.status_code} - {error_msg}")
                return False, f"请求失败: {error_msg[:100]}"

            # 获取任务ID
            task_response = response.json()
            if "task_id" not in task_response:
                logger.error(f"{self.log_prefix} (魔搭) 未获取到任务ID: {task_response}")
                return False, "未获取到任务ID"

            task_id = task_response["task_id"]
            logger.info(f"{self.log_prefix} (魔搭) 获得任务ID: {task_id}，开始轮询结果")

            # 轮询任务结果
            check_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-ModelScope-Task-Type": "image_generation"
            }

            # 优化轮询策略：前期快速轮询，后期降低频率
            # 总共最多 120 秒（2分钟）
            max_poll_time = 120  # 最大轮询时间（秒）
            poll_intervals = [2, 2, 3, 3, 5, 5, 5, 5, 5, 5, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10]  # 轮询间隔序列
            start_time = time.time()

            for poll_index, interval in enumerate(poll_intervals):
                # 检查是否超时
                if time.time() - start_time > max_poll_time:
                    logger.error(f"{self.log_prefix} (魔搭) 任务超时（{max_poll_time}秒），未能在规定时间内完成")
                    return False, f"任务执行超时（超过{max_poll_time}秒）"

                try:
                    status_url = f"{base_url}/tasks/{task_id}"
                    check_kwargs = {
                        "url": status_url,
                        "headers": check_headers,
                        "timeout": 10
                    }

                    if proxy_config:
                        check_kwargs["proxies"] = {
                            "http": proxy_config["http"],
                            "https": proxy_config["https"]
                        }

                    check_response = requests.get(**check_kwargs)

                    if check_response.status_code != 200:
                        logger.warning(f"{self.log_prefix} (魔搭) 状态检查失败: HTTP {check_response.status_code}")
                        continue

                    result_data = check_response.json()
                    task_status = result_data.get("task_status", "UNKNOWN")

                    if task_status == "SUCCEED":
                        if "output_images" in result_data and result_data["output_images"]:
                            image_url = result_data["output_images"][0]

                            # 下载图片并转换为base64
                            try:
                                img_kwargs = {"url": image_url, "timeout": 30}
                                if proxy_config:
                                    img_kwargs["proxies"] = {
                                        "http": proxy_config["http"],
                                        "https": proxy_config["https"]
                                    }

                                img_response = requests.get(**img_kwargs)
                                if img_response.status_code == 200:
                                    image_base64 = base64.b64encode(img_response.content).decode('utf-8')
                                    logger.info(f"{self.log_prefix} (魔搭) 图片生成成功")
                                    return True, image_base64
                                else:
                                    logger.error(f"{self.log_prefix} (魔搭) 图片下载失败: HTTP {img_response.status_code}")
                                    return False, "图片下载失败"
                            except Exception as e:
                                logger.error(f"{self.log_prefix} (魔搭) 图片下载异常: {e}")
                                return False, f"图片下载异常: {str(e)}"
                        else:
                            logger.error(f"{self.log_prefix} (魔搭) 未找到生成的图片")
                            return False, "未找到生成的图片"

                    elif task_status == "FAILED":
                        error_msg = result_data.get("error_message", "任务执行失败")
                        logger.error(f"{self.log_prefix} (魔搭) 任务失败: {error_msg}")
                        return False, f"任务执行失败: {error_msg}"

                    elif task_status in ["PENDING", "RUNNING", "PROCESSING"]:
                        logger.info(f"{self.log_prefix} (魔搭) 任务状态: {task_status}，等待中...")
                        time.sleep(interval)
                        continue

                    else:
                        logger.warning(f"{self.log_prefix} (魔搭) 未知任务状态: {task_status}")
                        time.sleep(interval)
                        continue

                except Exception as e:
                    logger.warning(f"{self.log_prefix} (魔搭) 状态检查异常: {e}")
                    time.sleep(interval)
                    continue

            logger.error(f"{self.log_prefix} (魔搭) 任务超时（{max_poll_time}秒），未能在规定时间内完成")
            return False, f"任务执行超时（超过{max_poll_time}秒）"

        except Exception as e:
            logger.error(f"{self.log_prefix} (魔搭) 请求异常: {e!r}", exc_info=True)
            return False, f"请求失败: {str(e)}"
