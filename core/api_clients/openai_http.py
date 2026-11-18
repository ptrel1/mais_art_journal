"""OpenAI 兼容客户端共用的认证与 HTTP 错误处理。"""

from typing import Tuple
from urllib.error import HTTPError

import json
import re

from .base_client import NonRetryableError, logger


def normalize_authorization(api_key: str) -> str:
    """裸 token 补 Bearer；保留用户显式填写的认证方案。"""
    if any(char in api_key for char in ("\r", "\n", "\x00")):
        raise NonRetryableError("OpenAI API 密钥含无效控制字符")
    value = api_key.strip()
    if value and len(value.split()) == 1:
        return f"Bearer {value}"
    return value


def _safe_error_text(body: str, authorization: str) -> str:
    """优先展示服务端错误码，并在截断前隐藏密钥及长图片数据。"""
    try:
        content = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        content = None
    if isinstance(content, dict) and "error" in content:
        detail = content["error"]
        if isinstance(detail, dict):
            # message 可能很长，先放 code/type，保证用户提示仍能包含错误码。
            detail = {key: detail[key] for key in ("code", "type", "message") if key in detail} or detail
        body = json.dumps(detail, ensure_ascii=False)

    if authorization:
        secrets = {authorization, authorization.split(maxsplit=1)[-1]}
        for secret in sorted(secrets, key=len, reverse=True):
            body = body.replace(secret, "***")
            body = body.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "***")
    body = re.sub(r"[A-Za-z0-9+/]{200,}={0,2}", "[BASE64_DATA...]", body)
    return " ".join(body.split())


def handle_http_error(
    error: HTTPError,
    *,
    authorization: str,
    log_prefix: str,
    client_name: str,
    error_label: str,
) -> Tuple[bool, str]:
    """读取有界错误正文；认证/权限错误交给基类终止重试。"""
    try:
        with error:
            body = error.read(64 * 1024).decode("utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        body = f"错误正文读取失败（{type(exc).__name__}）"
    cleaned = _safe_error_text(body, authorization)
    logger.error(
        "%s (%s) API请求失败. 状态: %s. 正文: %s",
        log_prefix, client_name, error.code, cleaned[:500] or "（空正文）",
    )
    message = f"{error_label}(状态码 {error.code})"
    if cleaned:
        message += f"：{cleaned[:200]}"
    if error.code in (401, 403):
        raise NonRetryableError(message) from None
    return False, message
