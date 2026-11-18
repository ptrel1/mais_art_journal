"""按宿主中的真实用户消息限制智能生图，记录不会因超时或重载而重置。"""

from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

import asyncio
import sqlite3
import uuid

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext

T = TypeVar("T")


async def resolve_draw_message(ctx: "PluginContext", stream_id: str, msg_id: str) -> str:
    """LLM 提供的 ID 必须对应当前聊天最新的非机器人用户消息。"""
    if not stream_id or stream_id.strip() != stream_id or not msg_id or msg_id.strip() != msg_id:
        raise ValueError("智能生图需要当前聊天的真实用户消息 ID；请等待用户新的画图要求。")
    messages = await ctx.call_capability(
        "message.get_recent", chat_id=stream_id, limit=1, filter_mai=True,
        include_binary_data=False, timeout_ms=10000,
    )
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise ValueError("未能核验本次画图的用户消息，已停止生图；请等待用户新的画图要求。")
    message = messages[0]
    info = message.get("message_info", {})
    user = info.get("user_info", {}) if isinstance(info, dict) else {}
    if (
        message.get("session_id") != stream_id or message.get("message_id") != msg_id
        or message.get("is_notify") is True or not isinstance(user, dict) or not user.get("user_id")
    ):
        raise ValueError("触发 ID 不属于当前聊天最新的用户消息，已停止生图；请等待用户新的画图要求。")
    return message["message_id"]


class DrawLimiter:
    """SQLite 原子预占；只有明确尚未生成或投递的失败才删除预占记录。"""

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._lock = asyncio.Lock()

    @staticmethod
    async def _finish_io(operation: Callable[[], T]) -> tuple[T, bool]:
        pending = asyncio.create_task(asyncio.to_thread(operation))
        cancelled = False
        # 线程中的事务不能取消；重复取消也必须等提交完成后再释放锁。
        while True:
            try:
                return await asyncio.shield(pending), cancelled
            except asyncio.CancelledError:
                cancelled = True
                if pending.cancelled():
                    raise

    async def load(self, path: Path) -> None:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("智能生图次数记录必须使用 SDK 数据目录的绝对路径")
        async with self._lock:
            if self._path is not None:
                if self._path == path:
                    return
                raise RuntimeError("智能生图次数记录已加载，不能更换文件")
            _, cancelled = await self._finish_io(lambda: self._initialize(path))
            if cancelled:
                raise asyncio.CancelledError()
            self._path = path

    @staticmethod
    def _initialize(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"智能生图次数记录版本不兼容：{version}")
            db.execute("CREATE TABLE IF NOT EXISTS draws(token TEXT PRIMARY KEY, session_id TEXT NOT NULL, message_id TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS draw_message ON draws(session_id, message_id)")
            db.execute("PRAGMA user_version=1")

    def _storage_path(self) -> Path:
        if self._path is None:
            raise RuntimeError("智能生图次数记录尚未加载或已经关闭")
        return self._path

    async def acquire(self, stream_id: str, msg_id: str, limit: int) -> Optional[str]:
        if not stream_id or not msg_id or limit < 1:
            raise ValueError("智能生图预占需要消息、聊天流和正整数上限")
        async with self._lock:
            path = self._storage_path()
            token, cancelled = await self._finish_io(lambda: self._acquire(path, stream_id, msg_id, limit))
            if cancelled:
                # 取消发生在预占返回之前，调用方不可能开始生图，安全退回本次名额。
                if token is not None:
                    await self._finish_io(lambda: self._release(path, token))
                raise asyncio.CancelledError()
            return token

    @staticmethod
    def _acquire(path: Path, stream_id: str, msg_id: str, limit: int) -> Optional[str]:
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("SELECT COUNT(*) FROM draws WHERE session_id=? AND message_id=?", (stream_id, msg_id)).fetchone()[0]
            if used >= limit:
                return None
            token = uuid.uuid4().hex
            db.execute("INSERT INTO draws VALUES (?, ?, ?)", (token, stream_id, msg_id))
            return token

    async def release(self, token: str) -> None:
        async with self._lock:
            path = self._storage_path()
            _, cancelled = await self._finish_io(lambda: self._release(path, token))
            if cancelled:
                raise asyncio.CancelledError()

    @staticmethod
    def _release(path: Path, token: str) -> None:
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("DELETE FROM draws WHERE token=?", (token,))

    async def close(self) -> None:
        async with self._lock:
            self._path = None
