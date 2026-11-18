"""通过真实客户端请求构造验证认证、错误正文和重试行为，禁止联网。"""

from collections import deque
from io import BytesIO
from typing import List
from unittest.mock import AsyncMock
from urllib.error import HTTPError
from urllib.request import Request

import asyncio
import json
import pytest
import urllib.request

from mais_art_journal.core.api_clients.client_context import ClientContext
from mais_art_journal.core.api_clients.openai_chat_client import OpenAIChatClient
from mais_art_journal.core.api_clients.openai_client import OpenAIClient


IMAGE_URL = "https://offline.invalid/image.png"
SUCCESS_BODY = json.dumps({
    "data": [{"url": IMAGE_URL}],
    "choices": [{"message": {"content": f"![image]({IMAGE_URL})"}}],
}).encode()


class Response(BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status
        self.read_sizes: List[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class Transport:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, replies):
        self.replies = deque(replies)
        self.requests: List[Request] = []
        self.bodies: List[Response] = []
        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: self)

    def open(self, request: Request, **kwargs):
        self.requests.append(request)
        status, body = self.replies.popleft()
        response = Response(body, status)
        self.bodies.append(response)
        if status >= 400:
            raise HTTPError(request.full_url, status, "offline HTTP error", {}, response)
        return response


@pytest.fixture(params=[OpenAIClient, OpenAIChatClient], ids=["openai", "openai-chat"])
def client(request):
    return request.param(ClientContext())


def generate(client, api_key: str = "offline-secret", max_retries: int = 0):
    return asyncio.run(client.generate_image(
        "a cat", {"base_url": "https://offline.invalid/v1", "api_key": api_key, "model": "offline-model"},
        "1024x1024", max_retries=max_retries,
    ))


@pytest.mark.parametrize("api_key,expected", [
    ("offline-secret", "Bearer offline-secret"),
    ("  offline-secret  ", "Bearer offline-secret"),
    ("Bearer offline-secret", "Bearer offline-secret"),
    ("  Bearer offline-secret  ", "Bearer offline-secret"),
    ("bEaReR offline-secret", "bEaReR offline-secret"),
    ("Token offline-secret", "Token offline-secret"),
    ("Basic dXNlcjpwYXNz", "Basic dXNlcjpwYXNz"),
    ("Token\toffline-secret", "Token\toffline-secret"),
    ("", ""),
])
def test_authorization_and_successful_image(client, monkeypatch, api_key, expected):
    transport = Transport(monkeypatch, [(200, SUCCESS_BODY)])
    assert generate(client, api_key) == (True, IMAGE_URL)
    assert transport.requests[0].get_header("Authorization") == expected
    payload = json.loads(transport.requests[0].data)
    assert payload["model"] == "offline-model" and payload["size"] == "1024x1024"


@pytest.mark.parametrize("api_key", ["secret\r\nHeader: value", "secret\x00value"])
def test_invalid_header_never_sends_or_exposes_key(client, monkeypatch, api_key, caplog):
    transport = Transport(monkeypatch, [])
    ok, error = generate(client, api_key, max_retries=2)
    assert not ok and "控制字符" in error
    assert not transport.requests
    assert "secret" not in error + caplog.text


@pytest.mark.parametrize("status,attempts", [(401, 1), (403, 1), (429, 3), (500, 3)])
def test_http_error_details_and_retry_policy(client, monkeypatch, caplog, status, attempts):
    body = json.dumps({"error": {"code": "invalid_api_key", "message": "密钥或权限有误"}}).encode()
    transport = Transport(monkeypatch, [(status, body)] * attempts)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    ok, error = generate(client, max_retries=2)
    assert not ok and str(status) in error
    assert "invalid_api_key" in error and "密钥或权限有误" in error
    assert "invalid_api_key" in caplog.text
    assert len(transport.requests) == attempts
    assert sleep.await_count == attempts - 1
    assert all(response.read_sizes == [64 * 1024] and response.closed for response in transport.bodies)


@pytest.mark.parametrize("body,expected", [
    (b"upstream unavailable", "upstream unavailable"),
    (b'{"error": "gateway unavailable"}', "gateway unavailable"),
    (b'{"message": "invalid request"}', "invalid request"),
    (b"\xff gateway error", "gateway error"),
    (b"", "状态码 400"),
])
def test_http_error_handles_nonstandard_and_empty_bodies(client, monkeypatch, body, expected):
    Transport(monkeypatch, [(400, body)])
    ok, error = generate(client)
    assert not ok and "400" in error and expected in error


@pytest.mark.parametrize("api_key", ["offline-secret", "Token offline-secret"])
def test_error_body_redacts_key_and_image_before_truncating(client, monkeypatch, caplog, api_key):
    body = json.dumps({"error": {
        "message": f"Authorization: {api_key}; token=offline-secret; image=" + "A" * 1000,
        "code": "invalid_api_key",
    }}).encode()
    Transport(monkeypatch, [(401, body)])
    ok, error = generate(client, api_key)
    assert not ok and "invalid_api_key" in error
    assert "offline-secret" not in error + caplog.text
    assert "A" * 200 not in error + caplog.text
    assert "***" in error and "[BASE64_DATA...]" in error


def test_large_error_body_is_bounded(client, monkeypatch, caplog):
    transport = Transport(monkeypatch, [(400, b"gateway error: " + b"!" * 100000)])
    ok, error = generate(client)
    assert not ok and "gateway error" in error
    assert len(error) < 250
    assert len(caplog.text) < 1200
    assert transport.bodies[0].read_sizes == [64 * 1024]


def test_body_read_failure_preserves_status_and_stops_auth_retries(client, monkeypatch, caplog):
    transport = Transport(monkeypatch, [(403, b"unused")])
    monkeypatch.setattr(Response, "read", lambda *args: (_ for _ in ()).throw(OSError("offline-secret")))
    ok, error = generate(client, max_retries=2)
    assert not ok and "403" in error and "读取失败" in error
    assert len(transport.requests) == 1 and transport.bodies[0].closed
    assert "offline-secret" not in error + caplog.text
