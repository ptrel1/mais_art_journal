"""ComfyUI 工作流 API 客户端

通过本地或远程 ComfyUI 实例的 HTTP API 生成图片：
- 加载工作流 JSON 模板
- 替换占位符 → 提交任务 → 轮询结果 → 下载图片

配置映射：
  base_url  → ComfyUI 服务地址（如 http://127.0.0.1:8188）
  model     → 工作流文件名（相对 workflow/ 目录）
  api_key   → 不需要，留空
  seed      → 随机种子，-1 表示自动随机

工作流占位符（在 JSON 中使用 "${xxx}" 格式）：
  ${prompt}           ← 用户提示词 + custom_prompt_add
  ${seed}             ← seed 配置值（-1 时自动随机）
  ${negative_prompt}  ← negative_prompt_add
  ${steps}            ← num_inference_steps
  ${cfg}              ← guidance_scale
  ${width}            ← 从 size 解析的宽度
  ${height}           ← 从 size 解析的高度
  ${denoise}          ← 图生图降噪强度（strength）
  ${image}            ← 图生图输入图片（自动上传）
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

import base64
import io
import json
import os
import random
import time
import uuid
import urllib.request

from PIL import Image

from .base_client import BaseApiClient, NonRetryableError, logger


@dataclass(frozen=True)
class ComfyOutputImage:
    """history 中的一张输出图，保留 /view 所需的完整定位信息。"""

    filename: str
    subfolder: str = ""
    image_type: str = "output"


class ComfyUIClient(BaseApiClient):
    """ComfyUI 工作流 API 客户端"""

    format_name = "comfyui"

    def _build_opener(self) -> Tuple[urllib.request.OpenerDirector, int]:
        """构建 opener（支持代理），返回 (opener, wait_seconds)。"""
        proxy_config = self._get_proxy_config()
        if proxy_config:
            proxy_handler = urllib.request.ProxyHandler({
                'http': proxy_config['http'],
                'https': proxy_config['https']
            })
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()
        # ComfyUI 的生成等待与代理连接超时是两套不同的语义。代理关闭时也
        # 必须能够配置长工作流的轮询时间，不能再从 proxy.timeout 读取。
        try:
            wait_seconds = int(self.ctx.get_config("comfyui.wait_seconds", 600) or 600)
        except (TypeError, ValueError):
            wait_seconds = 600
        return opener, max(1, wait_seconds)

    def _get_client_id(self) -> str:
        """返回本机器人提交到 ComfyUI 时的归属标识（用于队列去重）"""
        return str(self.ctx.get_config("comfyui.client_id", "maibot-hilda"))

    def _queue_has_own_task(self, base_url: str, opener: urllib.request.OpenerDirector) -> bool:
        """检查 ComfyUI 队列中是否已有本机器人(client_id)的待处理任务。

        仅在队列中不存在本机器人任务时才允许新提交，避免重复请求滚雪球式堆积。
        """
        client_id = self._get_client_id()
        try:
            req = urllib.request.Request(f"{base_url}/queue", method="GET")
            with opener.open(req, timeout=10) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8"))
            for section in ("queue_running", "queue_pending"):
                for item in data.get(section, []):
                    # item 结构: [number, prompt_id, prompt, extra_data, ...]
                    if len(item) > 3 and isinstance(item[3], dict):
                        if str(item[3].get("client_id", "")) == client_id:
                            return True
            return False
        except Exception as e:
            logger.warning(f"{self.log_prefix} (ComfyUI) 队列检查异常，按无自己任务处理: {e}")
            return False

    def _cancel_own_task(self, base_url: str, prompt_id: str, opener: urllib.request.OpenerDirector) -> None:
        """从 ComfyUI 队列删除本机器人已提交但滞留的单个任务"""
        try:
            req = urllib.request.Request(
                f"{base_url}/queue",
                data=json.dumps({"delete": [prompt_id]}).encode("utf-8"),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with opener.open(req, timeout=10):
                logger.info(f"{self.log_prefix} (ComfyUI) 已删除滞留任务: {prompt_id[:8]}")
        except Exception as e:
            logger.warning(f"{self.log_prefix} (ComfyUI) 删除滞留任务失败: {prompt_id[:8]}: {e}")

    def _task_in_queue(self, base_url: str, prompt_id: str, opener: urllib.request.OpenerDirector) -> bool:
        """检查指定 prompt_id 是否仍滞留在 ComfyUI 队列中（排队或运行中）"""
        try:
            req = urllib.request.Request(f"{base_url}/queue", method="GET")
            with opener.open(req, timeout=10) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8"))
            for section in ("queue_running", "queue_pending"):
                for item in data.get(section, []):
                    # item 结构: [number, prompt_id, prompt, extra_data, ...]
                    if len(item) > 1 and item[1] == prompt_id:
                        return True
            return False
        except Exception:
            return False

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: float = None,
        input_image_base64: str = None
    ) -> Tuple[bool, str]:
        """通过 ComfyUI 工作流生成图片"""
        base_url = model_config.get("base_url", "http://127.0.0.1:8188").rstrip("/")
        workflow_name = model_config.get("model", "")
        # 图生图：当提供输入图片时，切换到专用的 img2img 工作流
        if input_image_base64:
            base_workflow = workflow_name
            if base_workflow in ("sdxl_txt2img_api.json", "comfyui"):
                workflow_name = "sdxl_img2img_api.json"
            elif base_workflow.endswith("_txt2img_api.json"):
                workflow_name = base_workflow.replace("_txt2img_api.json", "_img2img_api.json")
        seed = model_config.get("seed", -1)
        custom_prompt_add = model_config.get("custom_prompt_add", "")
        full_prompt = prompt + custom_prompt_add

        # 构建 opener（局部使用，不污染全局）
        opener, default_timeout = self._build_opener()

        # ---- 1. 定位工作流文件 ----
        if not workflow_name:
            return False, "未配置工作流文件名（model 字段）"

        if os.path.isabs(workflow_name):
            workflow_file = workflow_name
        else:
            plugin_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            workflow_file = os.path.join(plugin_dir, "workflow", workflow_name)

        if not os.path.exists(workflow_file):
            return False, f"工作流文件不存在: {workflow_file}"

        logger.info(f"{self.log_prefix} (ComfyUI) 加载工作流: {workflow_file}")

        try:
            with open(workflow_file, "r", encoding="utf-8") as f:
                workflow_template = f.read()
        except Exception as e:
            return False, f"读取工作流文件失败: {e}"

        if input_image_base64 and '"${image}"' not in workflow_template:
            return False, "ComfyUI 参考图未使用：工作流需包含 ${image} 占位符，或将 support_img2img 设为 false"

        # ---- 1.5 提交前去重检查（提前到上传之前，避免排队时白白上传图片）----
        # 若队列中已有本机器人正在排队/运行的任务，说明上一次任务还没轮到，
        # 属于"排队中"而非"失败"，不应重复提交新任务（否则会滚雪球式堆积）。
        if self._queue_has_own_task(base_url, opener):
            logger.warning(
                f"{self.log_prefix} (ComfyUI) 队列中已有本机器人任务，跳过重复提交，等待已有任务完成"
            )
            # [QUEUED] 标记：表示"排队中而非失败"，上层检测到直接返回、不触发重试（防堆积）
            return False, "[QUEUED] 当前已有画图任务在排队，请稍候"

        # ---- 2. 替换占位符 ----
        if seed == -1:
            seed = random.randint(1, 10_000_000_000)

        workflow_str = workflow_template.replace('"${prompt}"', json.dumps(full_prompt))
        workflow_str = workflow_str.replace('"${seed}"', str(seed))

        # negative_prompt_add → ${negative_prompt}
        negative_prompt = model_config.get("negative_prompt_add", "")
        workflow_str = workflow_str.replace('"${negative_prompt}"', json.dumps(negative_prompt))

        # num_inference_steps → ${steps}
        steps = model_config.get("num_inference_steps", 20)
        workflow_str = workflow_str.replace('"${steps}"', str(int(steps)))

        # guidance_scale → ${cfg}
        # 注意：工作流占位符必须替换成合法数值，故 -1（=不发送）在此类型下回退为默认 7
        cfg = model_config.get("guidance_scale", 7)
        try:
            if cfg is None or float(cfg) < 0:
                cfg = 7
        except (TypeError, ValueError):
            cfg = 7
        workflow_str = workflow_str.replace('"${cfg}"', str(float(cfg)))

        # size → ${width} / ${height}
        try:
            w, h = size.lower().split("x")
            width, height = int(w), int(h)
        except Exception:
            width, height = 1024, 1024
        workflow_str = workflow_str.replace('"${width}"', str(width))
        workflow_str = workflow_str.replace('"${height}"', str(height))

        # strength → ${denoise}（图生图降噪强度）
        if strength is not None:
            workflow_str = workflow_str.replace('"${denoise}"', str(float(strength)))

        # ---- 3. 图生图：按 LLM 协商的尺寸缩放/裁剪输入图再上传 ----
        # img2img 工作流用 LoadImage→VAEEncode，输出尺寸恒等于输入图尺寸。
        # 若不限制，输入原图(如 3024x4032)会直接决定生成尺寸，导致超大图。
        # 这里把输入图缩放到协商好的 width/height（并受 img2img_max_side 兜底），
        # 让 LLM 自主决定的尺寸真正生效，避免生成超出预期的超大图。
        if input_image_base64 and '"${image}"' in workflow_str:
            upload_b64 = input_image_base64
            max_side = int(self.ctx.get_config("img2img_max_side", 0) or 0)
            if width and height:
                upload_b64 = self._resize_image_b64_for_img2img(input_image_base64, width, height, max_side)
            uploaded = self._upload_image_sync(base_url, upload_b64, opener)
            if uploaded:
                workflow_str = workflow_str.replace('"${image}"', json.dumps(uploaded))
                logger.info(f"{self.log_prefix} (ComfyUI) 图片已上传: {uploaded} (目标 {width}x{height}, max_side={max_side or '不限'})")
            else:
                logger.warning(f"{self.log_prefix} (ComfyUI) 图片上传失败，${'{image}'} 占位符未替换")

        # ---- 4. 解析工作流 JSON ----
        try:
            workflow = json.loads(workflow_str)
        except json.JSONDecodeError as e:
            return False, f"工作流 JSON 解析失败: {e}"

        # ---- 5. 提交任务 ----
        logger.info(f"{self.log_prefix} (ComfyUI) 提交任务, seed={seed}, prompt={full_prompt[:60]}...")
        prompt_id = self._queue_prompt_sync(base_url, workflow, opener)
        if not prompt_id:
            return False, "提交任务到 ComfyUI 失败，请检查服务是否运行"

        logger.info(f"{self.log_prefix} (ComfyUI) 任务已提交, prompt_id={prompt_id}")

        # ---- 6. 轮询结果 ----
        output_image = self._poll_history_sync(base_url, prompt_id, opener, timeout=default_timeout)
        if not output_image:
            # 轮询超时后，检查该任务是否仍滞留于队列（还在排队/运行，而非失败）
            if self._task_in_queue(base_url, prompt_id, opener):
                # 任务还在队列里慢慢跑，非系统失败——不判为失败，避免上层重试重复提交
                logger.warning(
                    f"{self.log_prefix} (ComfyUI) 任务 {prompt_id[:8]} 仍在队列中运行，非失败；"
                    f"清理该滞留任务后返回排队等待提示"
                )
                self._cancel_own_task(base_url, prompt_id, opener)
                return False, "[QUEUED] 当前画图任务仍在排队/运行，请稍后再试"
            return False, "等待 ComfyUI 生成结果超时"

        logger.info(f"{self.log_prefix} (ComfyUI) 生成完成, filename={output_image.filename}")

        # ---- 7. 下载图片 ----
        image_b64 = self._download_image_sync(base_url, output_image, opener, prompt_id=prompt_id)

        logger.info(f"{self.log_prefix} (ComfyUI) 图片下载成功, base64 长度: {len(image_b64)}")
        return True, image_b64

    # ================================================================
    #  辅助方法
    # ================================================================

    def _queue_prompt_sync(self, base_url: str, workflow: dict, opener: urllib.request.OpenerDirector) -> Optional[str]:
        """同步提交工作流到 ComfyUI，返回 prompt_id（附带 client_id 便于队列去重）"""
        url = f"{base_url}/prompt"
        payload = json.dumps({"prompt": workflow, "client_id": self._get_client_id()}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with opener.open(req, timeout=30) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("prompt_id")
                else:
                    logger.error(f"{self.log_prefix} (ComfyUI) 提交任务失败, status={resp.status}")
        except Exception as e:
            logger.error(f"{self.log_prefix} (ComfyUI) 提交任务异常: {e}")
        return None

    def _poll_history_sync(self, base_url: str, prompt_id: str, opener: urllib.request.OpenerDirector, timeout: int = 600) -> Optional[ComfyOutputImage]:
        """同步轮询 ComfyUI history，等待任务完成并返回输出图片定位信息。"""
        url = f"{base_url}/history/{prompt_id}"
        start = time.time()

        while time.time() - start < timeout:
            try:
                req = urllib.request.Request(url, method="GET")
                with opener.open(req, timeout=10) as resp:
                    if resp.status == 200:
                        history = json.loads(resp.read().decode("utf-8"))
                        if prompt_id in history:
                            return self._extract_output_image(history[prompt_id])
            except Exception:
                pass  # 网络抖动，继续轮询
            time.sleep(1)

        logger.error(f"{self.log_prefix} (ComfyUI) 轮询超时 ({timeout}s)")
        return None

    @staticmethod
    def _extract_output_image(task_data: dict) -> Optional[ComfyOutputImage]:
        """从 history 条目中提取第一张图片的文件名、子目录和文件类型。"""
        outputs = task_data.get("outputs", {})
        if not isinstance(outputs, dict):
            return None
        for node_output in outputs.values():
            if not isinstance(node_output, dict) or not isinstance(node_output.get("images"), list):
                continue
            for image in node_output["images"]:
                if not isinstance(image, dict):
                    continue
                filename = image.get("filename")
                subfolder = image.get("subfolder", "")
                image_type = image.get("type", "output")
                if (
                    isinstance(filename, str) and filename
                    and isinstance(subfolder, str)
                    and isinstance(image_type, str) and image_type in {"output", "temp", "input"}
                ):
                    return ComfyOutputImage(filename, subfolder, image_type)
        return None

    @staticmethod
    def _encode_output_image(raw: bytes) -> str:
        """验证实际图片内容；HTTP 200 的 JSON 错误体不能被当成 base64 图片。"""
        try:
            error = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            error = None
        if isinstance(error, dict):
            raise ValueError(f"下载响应为 JSON：code={error.get('code')!r}, msg={error.get('msg', '')!r}")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                    raise ValueError(f"不支持的图片格式: {image.format}")
                image.verify()
            # verify 检查容器；解码检查 JPEG 等格式的截断像素数据。
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        except (OSError, ValueError, SyntaxError) as exc:
            raise ValueError("下载响应不是有效的 PNG/JPEG/GIF/WebP 图片") from exc
        return base64.b64encode(raw).decode("ascii")

    def _download_output_url(self, url: str, opener: urllib.request.OpenerDirector) -> str:
        """下载并验证一张图片；所有调用均在客户端工作线程内完成。"""
        with opener.open(urllib.request.Request(url, method="GET"), timeout=30) as response:
            if response.status != 200:
                raise ValueError(f"图片下载 HTTP 状态: {response.status}")
            return self._encode_output_image(response.read())

    @staticmethod
    def _runninghub_proxy(base_url: str) -> Optional[Tuple[str, str]]:
        """仅对 RunningHub 官方 HTTPS 代理启用产物接口，返回 origin 和代理密钥。"""
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "runninghub.ai", "www.runninghub.ai", "runninghub.cn", "www.runninghub.cn",
        }:
            return None
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "proxy" or not parts[1]:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")), unquote(parts[1])

    def _download_runninghub_output(
        self, proxy: Tuple[str, str], image: ComfyOutputImage,
        prompt_id: str, opener: urllib.request.OpenerDirector,
    ) -> str:
        """通过 RunningHub 当前任务的产物直链取图，不重新提交工作流。"""
        origin, api_key = proxy
        request = urllib.request.Request(
            f"{origin}/task/openapi/outputs",
            data=json.dumps({"apiKey": api_key, "taskId": prompt_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError(f"RunningHub 产物接口 HTTP 状态: {response.status}")
            result = json.loads(response.read())
        if not isinstance(result, dict):
            raise ValueError("RunningHub 产物接口必须返回 JSON 对象")
        if result.get("code") != 0:
            raise ValueError(f"RunningHub 产物接口：code={result.get('code')!r}, msg={result.get('msg', '')!r}")
        if not isinstance(result.get("data"), list):
            raise ValueError("RunningHub 产物接口未返回文件列表")
        candidates: Dict[str, str] = {}
        for item in result["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("fileUrl"), str):
                continue
            file_url = item["fileUrl"]
            parsed = urlsplit(file_url)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                continue
            file_type = str(item.get("fileType", "")).lower().lstrip(".")
            filename = unquote(parsed.path).rsplit("/", 1)[-1]
            if file_type and file_type not in {"png", "jpg", "jpeg", "gif", "webp", "image", "image/png", "image/jpeg", "image/gif", "image/webp"}:
                continue
            if not file_type and filename.lower().rsplit(".", 1)[-1] not in {"png", "jpg", "jpeg", "gif", "webp"}:
                continue
            candidates[file_url] = str(item.get("fileName") or filename)
        matches = [url for url, filename in candidates.items() if filename == image.filename]
        if len(matches) == 1:
            target = matches[0]
        elif not matches and len(candidates) == 1:
            target = next(iter(candidates))
        else:
            raise ValueError(f"RunningHub 产物无法唯一匹配图片 {image.filename!r}（候选 {len(candidates)} 个）")
        return self._download_output_url(target, opener)

    def _download_image_sync(
        self, base_url: str, image: ComfyOutputImage,
        opener: urllib.request.OpenerDirector, *, prompt_id: str,
    ) -> str:
        """验证 /view 响应并适配 RunningHub；失败只重试取图，禁止再次生图。"""
        query = urlencode({"filename": image.filename, "subfolder": image.subfolder, "type": image.image_type})
        view_url = f"{base_url}/view?{query}"
        proxy = self._runninghub_proxy(base_url)
        errors: Dict[str, str] = {}
        for attempt in range(2):
            try:
                return self._download_output_url(view_url, opener)
            except Exception as exc:
                errors["/view"] = str(exc)
            if proxy:
                try:
                    return self._download_runninghub_output(proxy, image, prompt_id, opener)
                except Exception as exc:
                    errors["RunningHub 产物"] = str(exc)
            # 地址可能含密钥；错误日志和对外提示都必须去掉它。
            detail = "; ".join(f"{source}: {error}" for source, error in errors.items()).replace(base_url, "[ComfyUI]")
            if proxy:
                detail = detail.replace(proxy[1], "[redacted]")
            logger.warning("%s (ComfyUI) 图片获取失败（第 %s 次）：%s", self.log_prefix, attempt + 1, detail[:500])
            if attempt == 0:
                time.sleep(1)
        raise NonRetryableError(f"ComfyUI 任务 {prompt_id} 已生成，图片获取失败：{detail[:500]}；不会自动重新提交生图任务")

    def _resize_image_b64_for_img2img(
        self,
        image_base64: str,
        target_w: int,
        target_h: int,
        max_side: int = 0,
    ) -> str:
        """图生图前把输入图缩放/裁剪到目标尺寸（cover 保构图），并受 max_side 兜底。

        img2img 的输出尺寸由输入图尺寸决定（LoadImage→VAEEncode），
        这里确保输入图缩放到 LLM 协商的尺寸，避免超大图。
        target=0 或解析失败时返回原图 base64。
        """
        if not target_w or not target_h:
            return image_base64

        # max_side 兜底：目标长边超限则等比收敛
        tw, th = target_w, target_h
        if max_side and max(tw, th) > max_side:
            scale = max_side / float(max(tw, th))
            tw, th = round(tw * scale), round(th * scale)

        clean_b64 = self._get_clean_base64(image_base64)
        try:
            img = Image.open(io.BytesIO(base64.b64decode(clean_b64))).convert("RGB")
        except Exception as e:
            logger.warning(f"{self.log_prefix} (ComfyUI) 解析输入图失败，按原图上传: {e}")
            return image_base64

        ow, oh = img.size
        target_ratio = tw / float(th)
        src_ratio = ow / float(oh)
        try:
            if src_ratio > target_ratio:
                # 原图过宽，裁剪左右以适配目标比例
                new_w = round(oh * target_ratio)
                x = (ow - new_w) // 2
                img = img.crop((x, 0, x + new_w, oh))
            else:
                # 原图过高，裁剪上下以适配目标比例
                new_h = round(ow / target_ratio)
                y = (oh - new_h) // 2
                img = img.crop((0, y, ow, y + new_h))
            img = img.resize((tw, th), Image.LANCZOS)
        except Exception as e:
            logger.warning(f"{self.log_prefix} (ComfyUI) 输入图缩放失败，按原图上传: {e}")
            return image_base64

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

    def _upload_image_sync(self, base_url: str, image_base64: str, opener: urllib.request.OpenerDirector) -> Optional[str]:
        """同步上传图片到 ComfyUI /upload/image，返回上传后的文件路径"""
        clean_b64 = self._get_clean_base64(image_base64)
        image_bytes = base64.b64decode(clean_b64)
        mime_type = self._detect_mime_type(clean_b64)

        ext_map = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }
        ext = ext_map.get(mime_type, "png")
        filename = f"upload_{uuid.uuid4().hex[:8]}.{ext}"
        subfolder = "temp"

        # 构建 multipart/form-data
        boundary = uuid.uuid4().hex
        body = b""

        # image 字段
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode()
        body += f"Content-Type: {mime_type}\r\n\r\n".encode()
        body += image_bytes
        body += b"\r\n"

        # subfolder 字段
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="subfolder"\r\n\r\n'
        body += subfolder.encode()
        body += b"\r\n"

        # overwrite 字段
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
        body += b"true\r\n"

        body += f"--{boundary}--\r\n".encode()

        url = f"{base_url}/upload/image"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

        try:
            with opener.open(req, timeout=60) as resp:
                if resp.status == 200:
                    result = json.loads(resp.read().decode("utf-8"))
                    name = result.get("name")
                    if name:
                        sub = result.get("subfolder", subfolder)
                        return f"{sub}/{name}" if sub else name
                    logger.error(f"{self.log_prefix} (ComfyUI) 上传响应缺少 name: {result}")
                else:
                    logger.error(f"{self.log_prefix} (ComfyUI) 上传图片失败, status={resp.status}")
        except Exception as e:
            logger.error(f"{self.log_prefix} (ComfyUI) 上传图片异常: {e}")
        return None
