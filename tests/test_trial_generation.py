"""绘图的后台执行、原图传输、参考图边界与卸载生命周期。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import base64
import json
import pytest

from maibot_sdk import PluginPaths
from PIL import Image

from mais_art_journal.core.webui.trial import CHUNK_SIZE
from mais_art_journal.plugin import MaisArtPlugin


REQUEST_ID = "a" * 32


def image_data(edge: int = 8) -> bytes:
    output = BytesIO()
    Image.new("RGB", (edge, edge), "blue").save(output, format="PNG", compress_level=0)
    return output.getvalue()


def make_plugin(tmp_path: Path) -> MaisArtPlugin:
    plugin = MaisArtPlugin()
    plugin.set_plugin_config(plugin.build_default_config())
    plugin._set_context(SimpleNamespace(
        paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"),
        call_capability=AsyncMock(), send=SimpleNamespace(image=AsyncMock(), text=AsyncMock()),
    ))
    return plugin


def success(data: bytes) -> dict:
    return {"success": True, "image_base64": base64.b64encode(data).decode(), "model_id": "model1", "size": "1024x1024", "is_img2img": False}


def test_start_is_short_idempotent_and_allows_only_one_running_job(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        gate = asyncio.Event()
        async def generate(**kwargs):
            await gate.wait()
            return success(image_data())
        plugin.api_generate_image = AsyncMock(side_effect=generate)
        first = await plugin.trial_start('{"prompt":"a cat"}', REQUEST_ID)
        assert first["job"]["state"] == "running"
        await asyncio.sleep(0)
        again = await plugin.trial_start('{"prompt":"a cat"}', REQUEST_ID)
        rejected = await plugin.trial_start('{"prompt":"another cat"}', "b" * 32)
        assert again["success"] and again["job"]["id"] == REQUEST_ID
        assert not rejected["success"] and rejected["job"]["id"] == REQUEST_ID
        assert plugin.api_generate_image.await_count == 1
        assert (await plugin.trial_status())["job"]["state"] == "running"
        gate.set()
        await plugin._trial_manager.task
        assert (await plugin.trial_status())["job"]["state"] == "ready"
    asyncio.run(run())


def test_original_image_round_trips_in_bounded_chunks(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        original = image_data(400)
        plugin.api_generate_image = AsyncMock(return_value=success(original))
        await plugin.trial_start('{"prompt":"a cat"}', REQUEST_ID)
        await plugin._trial_manager.task
        status = await plugin.trial_status(REQUEST_ID)
        job = status["job"]
        assert job["chunks"] > 1 and "image_base64" not in job
        chunks = []
        for index in range(job["chunks"]):
            chunk = await plugin.trial_result(REQUEST_ID, index)
            assert len(chunk["data"]) <= CHUNK_SIZE
            assert len(json.dumps(chunk).encode()) < 524288
            chunks.append(chunk["data"])
        assert base64.b64decode("".join(chunks)) == original
        assert not (await plugin.trial_result(REQUEST_ID, -1))["success"]
        assert not (await plugin.trial_result("b" * 32, 0))["success"]
    asyncio.run(run())


def test_trial_uses_generation_pipeline_without_chat_delivery_or_cache(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        requests = []
        async def pipeline_run(request, ctx):
            requests.append(request)
            request.success = True
            request.resolved_image_data = base64.b64encode(image_data()).decode()
            request.final_size = "1024x1024"
            return request
        plugin._action_pipeline = SimpleNamespace(run=AsyncMock(side_effect=pipeline_run))
        await plugin.trial_start('{"prompt":"a cat","model_id":"model1"}', REQUEST_ID)
        await plugin._trial_manager.task
        request = requests[0]
        assert request.source == "standalone"
        assert not request.send_image and not request.update_cache and not request.schedule_recall
        plugin.ctx.send.image.assert_not_awaited()
        assert not plugin._active_operations
    asyncio.run(run())


def test_reference_upload_passes_image_and_strength_and_removes_temp_file(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        uploads = plugin.ctx.paths.data_dir / "uploads"
        uploads.mkdir(parents=True)
        source = uploads / "reference.png"
        original = image_data()
        source.write_bytes(original)
        plugin.ctx.call_capability.return_value = {"path": str(source)}
        uploaded = await plugin.trial_upload("upload-id", "reference.png")
        assert uploaded["success"] and not source.exists()
        plugin.api_generate_image = AsyncMock(return_value=success(original))
        request = {"prompt": "change the scene", "reference_id": uploaded["reference_id"], "strength": 0.25}
        await plugin.trial_start(json.dumps(request), REQUEST_ID)
        await plugin._trial_manager.task
        args = plugin.api_generate_image.call_args.kwargs
        assert base64.b64decode(args["input_image_base64"]) == original
        assert args["strength"] == 0.25 and args["use_cache"] is False
    asyncio.run(run())


def test_reference_upload_does_not_read_or_delete_outside_granted_directory(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        source = tmp_path / "outside.png"
        source.write_bytes(image_data())
        plugin.ctx.call_capability.return_value = {"path": str(source)}
        assert not (await plugin.trial_upload("upload-id"))["success"]
        assert source.exists() and not plugin._trial_manager.reference_id
    asyncio.run(run())


def test_removed_reference_is_not_restored_or_reused(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        source = tmp_path / "reference.png"
        source.write_bytes(image_data())
        uploaded = await plugin._trial_manager.upload(source)
        plugin.api_generate_image = AsyncMock(return_value=success(image_data()))
        payload = json.dumps({"prompt": "a cat", "reference_id": uploaded["reference_id"]})
        await plugin.trial_start(payload, REQUEST_ID)
        await plugin._trial_manager.task
        assert (await plugin.trial_remove_reference(uploaded["reference_id"]))["success"]
        assert (await plugin.trial_status())["reference_id"] == ""
        assert not plugin._trial_manager.reference_base64
        assert not (await plugin.trial_start(payload, "b" * 32))["success"]
        assert (await plugin.trial_result(REQUEST_ID, 0))["success"]
    asyncio.run(run())


@pytest.mark.parametrize("patch", [
    {"prompt": " "}, {"model_id": "unsaved-model"}, {"reference_id": "f" * 32},
    {"strength": 2}, {"strength": True}, {"size": "invalid"}, {"unknown": True},
])
def test_invalid_requests_do_not_start_generation(tmp_path, patch):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.api_generate_image = AsyncMock()
        result = await plugin.trial_start(json.dumps({"prompt": "a cat", **patch}), REQUEST_ID)
        assert not result["success"] and plugin._trial_manager.task is None
        plugin.api_generate_image.assert_not_awaited()
    asyncio.run(run())


def test_failed_generation_surfaces_error_without_image_chunks(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.api_generate_image = AsyncMock(return_value={"success": False, "error": "invalid_api_key"})
        await plugin.trial_start('{"prompt":"a cat"}', REQUEST_ID)
        await plugin._trial_manager.task
        job = (await plugin.trial_status())["job"]
        assert job["state"] == "failed" and job["error"] == "invalid_api_key"
        assert job["chunks"] == 0
    asyncio.run(run())


def test_cancel_before_first_task_step_records_cancelled_state(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.api_generate_image = AsyncMock()
        await plugin.trial_start('{"prompt":"a cat"}', REQUEST_ID)
        cancelled = await plugin.trial_cancel(REQUEST_ID)
        assert cancelled["job"]["state"] == "cancelled"
        plugin.api_generate_image.assert_not_awaited()
    asyncio.run(run())


def test_unload_finishes_background_task_and_clears_reference(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        stopped = asyncio.Event()
        async def generate(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        plugin.api_generate_image = AsyncMock(side_effect=generate)
        await plugin.trial_start('{"prompt":"a cat"}', REQUEST_ID)
        await asyncio.sleep(0)
        task = plugin._trial_manager.task
        plugin._trial_manager.reference_base64 = "reference"
        await plugin.on_unload()
        assert stopped.is_set() and task.done()
        assert not plugin._trial_manager.reference_base64 and plugin._trial_manager.job is None
        with pytest.raises(RuntimeError):
            await plugin.trial_start('{"prompt":"a cat"}', "b" * 32)
    asyncio.run(run())
