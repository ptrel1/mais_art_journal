"""用真实 SQLite 与 SDK 替身验证额度门禁、投递回执及命令权限。"""

from io import BytesIO
from maibot_sdk import PluginPaths
from pathlib import Path
from PIL import Image
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import asyncio
import base64
import pytest

from mais_art_journal.core.config.models import MaisArtConfig
from mais_art_journal.core.pipeline import Pipeline
from mais_art_journal.core.pipeline.request import GenerationRequest
from mais_art_journal.core.pipeline.result import StepResult
from mais_art_journal.core.pipeline.steps.send_image import SendImage
from mais_art_journal.core.quota import QuotaPolicy, QuotaSubject
from mais_art_journal.core.quota.service import resolve_quota_context
from mais_art_journal.plugin import MaisArtPlugin


SESSION = "host-registered-group-session"
PRIVATE_SESSION = "host-registered-private-session"
SUBJECT = QuotaSubject("qq", "group", "1001")
POLICY = QuotaPolicy(1)


async def open_plugin(path: Path, *, limit: int = 1) -> MaisArtPlugin:
    config = MaisArtConfig()
    config.plugin.enabled = True
    config.basic.admin_users = ["admin"]
    config.basic.prompt_optimizer_enabled = False
    config.basic.cache_enabled = False
    config.quota.enabled = True
    config.quota.group_limit = limit
    config.quota.private_limit = limit
    plugin = MaisArtPlugin()
    plugin._plugin_dir = str(path)
    plugin.set_plugin_config(config.model_dump(mode="json"))
    plugin._set_context(SimpleNamespace(
        paths=PluginPaths(data_dir=path / "sdk-data", runtime_dir=path / "sdk-runtime"),
        call_capability=AsyncMock(return_value=[{
            "session_id": SESSION, "platform": "qq", "chat_type": "group", "group_id": "1001", "group_name": "绘画群",
        }, {
            "session_id": PRIVATE_SESSION, "platform": "qq", "chat_type": "private", "user_id": "2002", "user_nickname": "小明",
        }]),
        send=SimpleNamespace(
            image=AsyncMock(return_value={"sent": True, "message_id": "generated-message"}),
            text=AsyncMock(return_value=True),
        ),
        llm=SimpleNamespace(generate=AsyncMock()),
    ))
    await plugin.on_load()
    return plugin


def request(**kwargs: Any) -> GenerationRequest:
    fields: Dict[str, Any] = {
        "description": "一只小猫", "stream_id": SESSION, "chat_id": SESSION,
        "group_id": "1001", "user_id": "2002", "platform": "qq", "debug_info": False,
    }
    fields.update(kwargs)
    return GenerationRequest(**fields)


def generated_step() -> Any:
    async def generate(req: GenerationRequest, ctx: Any) -> None:
        req.resolved_image_data = "offline-generated-image"
    return SimpleNamespace(name="offline-generate", run=AsyncMock(side_effect=generate))


@pytest.mark.parametrize("source", ["tool", "cmd_style", "cmd_natural"])
def test_all_chat_sources_use_same_budget_and_deny_before_generation(tmp_path: Path, source: str) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        try:
            generated = generated_step()
            pipeline = Pipeline([generated, SendImage()])
            result = await pipeline.run(request(source=source), SimpleNamespace(plugin=plugin))
            assert result.success and result.delivery_state == "sent"
            status = await plugin.quota_store.status(SUBJECT, POLICY)
            assert (status.remaining, status.consumed, status.reserved) == (0, 1, 0)
            other = await pipeline.run(request(source="cmd_natural", user_id="another-member"), SimpleNamespace(plugin=plugin))
            assert not other.success and "额度已用完" in other.error
            assert generated.run.await_count == 1
            assert plugin.ctx.send.image.await_count == 1
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,consumed,reserved,unknown", [
    ({"sent": True, "message_id": "generated-message"}, 1, 0, 0),
    ({"sent": False, "message_id": None}, 0, 1, 1),
    (None, 0, 1, 1), (True, 0, 1, 1), ("true", 0, 1, 1),
])
def test_only_explicit_sdk_receipts_decide_refund(tmp_path: Path, outcome: Any, consumed: int, reserved: int, unknown: int) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        plugin.ctx.send.image.return_value = outcome
        try:
            await Pipeline([generated_step(), SendImage()]).run(request(), SimpleNamespace(plugin=plugin))
            status = await plugin.quota_store.status(SUBJECT, POLICY)
            assert (status.consumed, status.reserved, status.unknown) == (consumed, reserved, unknown)
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


def test_generation_failure_returns_reserved_credit(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        try:
            failure = SimpleNamespace(name="failure", run=AsyncMock(return_value=StepResult.fail(error="离线生成失败")))
            result = await Pipeline([failure, SendImage()]).run(request(), SimpleNamespace(plugin=plugin))
            assert not result.success
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 1
            plugin.ctx.send.image.assert_not_awaited()
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


@pytest.mark.parametrize("during_send", [False, True])
def test_cancellation_refunds_only_before_delivery_starts(tmp_path: Path, during_send: bool) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        started = asyncio.Event()
        async def blocked(*args: Any, **kwargs: Any) -> None:
            started.set()
            await asyncio.Event().wait()
        generate = generated_step()
        if during_send:
            plugin.ctx.send.image.side_effect = blocked
        else:
            generate.run.side_effect = blocked
        try:
            task = asyncio.create_task(Pipeline([generate, SendImage()]).run(request(), SimpleNamespace(plugin=plugin)))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            status = await plugin.quota_store.status(SUBJECT, POLICY)
            assert status.consumed == 0
            assert status.remaining == (0 if during_send else 1)
            assert status.unknown == int(during_send)
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


def test_repeated_cancellation_during_reserve_does_not_lose_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        committed, release = asyncio.Event(), asyncio.Event()
        real_reserve = plugin.quota_store.reserve
        async def delayed(*args: Any) -> Any:
            reservation = await real_reserve(*args)
            committed.set()
            await release.wait()
            return reservation
        monkeypatch.setattr(plugin.quota_store, "reserve", delayed)
        req = request()
        try:
            task = asyncio.create_task(Pipeline([generated_step(), SendImage()]).run(req, SimpleNamespace(plugin=plugin)))
            await committed.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert req.quota_reservation_id
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 1
            plugin.ctx.send.image.assert_not_awaited()
        finally:
            release.set()
            await plugin.on_unload()
    asyncio.run(scenario())


def test_repeated_cancellation_during_settlement_waits_for_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        real_settle = plugin.quota_store.settle
        async def delayed(*args: Any) -> Any:
            started.set()
            await release.wait()
            return await real_settle(*args)
        monkeypatch.setattr(plugin.quota_store, "settle", delayed)
        try:
            task = asyncio.create_task(Pipeline([generated_step(), SendImage()]).run(request(), SimpleNamespace(plugin=plugin)))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            status = await plugin.quota_store.status(SUBJECT, POLICY)
            assert (status.consumed, status.reserved) == (1, 0)
        finally:
            release.set()
            await plugin.on_unload()
    asyncio.run(scenario())


@pytest.mark.parametrize("raises", [False, True])
def test_confirmed_delivery_still_succeeds_when_later_step_fails(tmp_path: Path, raises: bool) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        try:
            failure = AsyncMock(side_effect=RuntimeError("撤回调度失败")) if raises else AsyncMock(
                return_value=StepResult.fail(error="后处理失败", user_message="不应发送这条失败提示"),
            )
            failing = SimpleNamespace(name="recall", run=failure)
            result = await Pipeline([generated_step(), SendImage(), failing]).run(request(cache_hit=True), SimpleNamespace(plugin=plugin))
            assert result.success and "图片已发送" in result.user_message
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).consumed == 1
            plugin.ctx.send.text.assert_not_awaited()
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"stream_id": "fabricated-session"}, {"platform": "other"}, {"group_id": "other-group"},
    {"group_id": "", "user_id": "2002"}, {"platform": ""},
])
def test_unverified_session_cannot_start_or_debit(tmp_path: Path, overrides: Dict[str, str]) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        try:
            generate = generated_step()
            result = await Pipeline([generate, SendImage()]).run(request(**overrides), SimpleNamespace(plugin=plugin))
            assert not result.success
            generate.run.assert_not_awaited()
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 1
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


def test_private_budget_uses_existing_session_and_name(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        try:
            context = await resolve_quota_context(plugin, stream_id=PRIVATE_SESSION, platform="qq", group_id="", user_id="2002")
            assert context.display_name == "小明的私聊"
            req = request(stream_id=PRIVATE_SESSION, chat_id=PRIVATE_SESSION, group_id="")
            assert (await Pipeline([generated_step(), SendImage()]).run(req, SimpleNamespace(plugin=plugin))).success
            assert (await plugin.quota_store.status(QuotaSubject("qq", "user", "2002"), POLICY)).consumed == 1
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 1
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


def test_admin_commands_are_scoped_and_settle_unknown_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        entry = await plugin.quota_store.reserve(SUBJECT, POLICY)
        await plugin.quota_store.settle(entry.token, "unknown")
        await plugin.on_unload()
        plugin = await open_plugin(tmp_path)
        async def command(rest: str, user: str = "admin") -> Any:
            return await plugin.handle_dr(stream_id=SESSION, platform="qq", group_id="1001", user_id=user, matched_groups={"rest": rest})
        try:
            assert (await command("quota adjust +5", user="not-admin"))[0] is False
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).adjustment == 0
            assert (await command("额度"))[0]
            assert "绘画群" in plugin.ctx.send.text.await_args.args[0]
            assert (await command("quota pending"))[0]
            assert entry.token in plugin.ctx.send.text.await_args.args[0]
            assert (await command(f"quota resolve {entry.token} refund"))[0]
            assert (await command(f"quota resolve {entry.token} refund"))[0]
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 1
            assert (await command("quota adjust +2 加餐"))[0]
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 3
            other = await plugin.quota_store.reserve(QuotaSubject("qq", "group", "9999"), POLICY)
            await plugin.quota_store.settle(other.token, "unknown")
            assert (await command(f"quota resolve {other.token} refund"))[0] is False
            # 关闭生图后仍可核对，但状态 reset 不得清空额度。
            await plugin.runtime_state.set_plugin_enabled(SESSION, False)
            assert (await command("quota pending"))[0]
            assert (await command("reset"))[0]
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).remaining == 3
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


def test_native_novelai_public_api_returns_image_without_chat_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_file = BytesIO()
    Image.new("RGB", (128, 128), "pink").save(image_file, format="PNG")
    png = image_file.getvalue()
    class Response:
        status_code = 200
        headers = {"Content-Type": "image/png", "Content-Length": str(len(png))}
        def __enter__(self) -> "Response":
            return self
        def __exit__(self, *args: Any) -> None:
            return None
        def iter_content(self, chunk_size: int) -> Any:
            yield png
    http = Mock(return_value=Response())
    monkeypatch.setattr("mais_art_journal.core.api_clients.novelai_client.requests.post", http)

    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        config = plugin.config.model_dump(mode="json")
        config["models"]["items"] = [{
            "id": "model1", "format": "novelai", "model": "nai-diffusion-4-5-full",
            "base_url": "https://image.novelai.net", "api_key": "offline-test-token",
            "fixed_size_enabled": True, "default_size": "128x128", "custom_prompt_add": "",
        }]
        plugin.set_plugin_config(config)
        try:
            output = await plugin.api_generate_image(prompt="a small cat")
            assert output["success"], output["error"]
            assert base64.b64decode(output["image_base64"]) == png
            http.assert_called_once()
            assert http.call_args.kwargs["json"]["parameters"]["n_samples"] == 1
            plugin.ctx.send.image.assert_not_awaited()
            plugin.ctx.call_capability.assert_not_awaited()
            assert (await plugin.quota_store.status(SUBJECT, POLICY)).consumed == 0
        finally:
            await plugin.on_unload()
    asyncio.run(scenario())


def test_unload_drains_active_tool_and_finishes_accounting_before_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        sending, settling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        req = request()
        async def blocked_send(*args: Any, **kwargs: Any) -> None:
            sending.set()
            await asyncio.Event().wait()
        real_settle = plugin.quota_store.settle
        async def delayed_settle(*args: Any) -> Any:
            settling.set()
            await finish.wait()
            return await real_settle(*args)
        plugin.ctx.send.image.side_effect = blocked_send
        plugin._action_pipeline = Pipeline([generated_step(), SendImage()])
        monkeypatch.setattr("mais_art_journal.core.pipeline.build_request_from_tool_kwargs", AsyncMock(return_value=req))
        monkeypatch.setattr("mais_art_journal.core.pipeline.make_pipeline_context", lambda plugin, req: SimpleNamespace(plugin=plugin))
        monkeypatch.setattr(plugin.quota_store, "settle", delayed_settle)
        streams = plugin.ctx.call_capability.return_value
        async def capability(name: str, **kwargs: Any) -> Any:
            if name == "message.get_recent":
                return [{"message_id": "trigger-message", "session_id": SESSION,
                         "message_info": {"user_info": {"user_id": "2002"}}}]
            return streams
        plugin.ctx.call_capability.side_effect = capability
        running = asyncio.create_task(plugin.handle_draw_picture(stream_id=SESSION, msg_id="trigger-message"))
        await asyncio.wait_for(sending.wait(), 5)
        unloading = asyncio.create_task(plugin.on_unload())
        await asyncio.wait_for(settling.wait(), 5)
        assert plugin.quota_store.loaded
        unloading.cancel()
        await asyncio.sleep(0)
        unloading.cancel()
        await asyncio.sleep(0)
        assert plugin.quota_store.loaded
        with pytest.raises(RuntimeError, match="正在卸载"):
            await plugin.api_generate_image(prompt="不能再接单")
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        with pytest.raises(asyncio.CancelledError):
            await unloading
        assert not plugin._active_operations and not plugin.quota_store.loaded
        reopened = await open_plugin(tmp_path)
        try:
            status = await reopened.quota_store.status(SUBJECT, POLICY)
            assert (status.unknown, status.reserved, status.remaining) == (1, 1, 0)
        finally:
            await reopened.on_unload()
    asyncio.run(scenario())


def test_sdk_metadata_survives_operation_tracking() -> None:
    plugin = MaisArtPlugin()
    components = {item["name"]: item for item in plugin.get_components()}
    assert components["generate_image"]["metadata"]["timeout_ms"] == 660000
    assert components["draw_picture"]["metadata"]["timeout_ms"] == 660000
    assert components["dr"]["metadata"]["timeout_ms"] == 660000


def test_unload_prevents_concurrent_config_reload_from_restarting_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = await open_plugin(tmp_path)
        config = plugin.config.model_dump(mode="json")
        config["selfie"]["auto_enabled"] = True
        plugin.set_plugin_config(config)
        stopping = asyncio.Event()
        count = 0
        async def stop() -> None:
            nonlocal count
            count += 1
            if count == 1:
                stopping.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # 与后台任务 stop 的契约一致：取消旧循环后正常返回。
                    pass
        task = SimpleNamespace(start=AsyncMock(), stop=stop)
        plugin._auto_selfie_task = task
        reloading = asyncio.create_task(plugin.on_config_update("self", config, "4.4.0"))
        await stopping.wait()
        await plugin.on_unload()
        await reloading
        task.start.assert_not_awaited()
        assert count == 2 and not plugin._active_operations and not plugin.quota_store.loaded
    asyncio.run(scenario())
