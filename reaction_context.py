"""Durable Telegram message context for reaction-triggered turns.

Telegram reaction updates identify the target message but do not include its text or
thread. The adapter records inbound and outbound messages here so a later reaction
can be routed to the exact topic and quoted to the agent, including after restart.
All operations are best-effort: indexing must never break Telegram delivery.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from typing import Any

_MAX_ENTRIES = 5000
_MAX_TEXT_CHARS = 32768
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_context (
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    thread_id TEXT,
    is_own INTEGER NOT NULL,
    topic TEXT,
    skill TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
)
"""


def _store_path() -> str:
    from hermes_constants import get_hermes_home

    return os.path.join(str(get_hermes_home()), "state", "telegram_reaction_context.sqlite3")


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(_SCHEMA)
    return connection


def record(
    chat_id: object,
    message_id: object,
    text: str | None,
    *,
    thread_id: object = None,
    is_own: bool = False,
    topic: str | None = None,
    skill: str | None = None,
) -> None:
    """Upsert one bounded message snapshot without rewriting the complete index."""
    if chat_id is None or message_id is None or not text:
        return
    with _LOCK:
        try:
            with _connect(_store_path()) as connection:
                connection.execute(
                    """
                    INSERT INTO message_context (
                        chat_id, message_id, text, thread_id, is_own, topic, skill, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id) DO UPDATE SET
                        text = excluded.text,
                        thread_id = COALESCE(excluded.thread_id, message_context.thread_id),
                        is_own = excluded.is_own,
                        topic = COALESCE(excluded.topic, message_context.topic),
                        skill = COALESCE(excluded.skill, message_context.skill),
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(chat_id),
                        str(message_id),
                        str(text)[:_MAX_TEXT_CHARS],
                        str(thread_id) if thread_id is not None and str(thread_id) else None,
                        int(bool(is_own)),
                        str(topic) if topic else None,
                        str(skill) if skill else None,
                        int(time.time()),
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM message_context
                    WHERE rowid IN (
                        SELECT rowid FROM message_context
                        ORDER BY updated_at ASC
                        LIMIT MAX((SELECT COUNT(*) FROM message_context) - ?, 0)
                    )
                    """,
                    (_MAX_ENTRIES,),
                )
        except (OSError, sqlite3.Error):
            return


async def record_async(*args: Any, **kwargs: Any) -> None:
    """Run the SQLite operation away from the gateway event loop."""
    await asyncio.to_thread(record, *args, **kwargs)


def record_if_absent(
    chat_id: object,
    message_id: object,
    text: str | None,
    *,
    thread_id: object = None,
    is_own: bool = False,
    topic: str | None = None,
    skill: str | None = None,
) -> None:
    """Insert reply-derived context without replacing a more authoritative snapshot."""
    if chat_id is None or message_id is None or not text:
        return
    with _LOCK:
        try:
            with _connect(_store_path()) as connection:
                connection.execute(
                    """
                    INSERT INTO message_context (
                        chat_id, message_id, text, thread_id, is_own, topic, skill, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id) DO NOTHING
                    """,
                    (
                        str(chat_id),
                        str(message_id),
                        str(text)[:_MAX_TEXT_CHARS],
                        str(thread_id) if thread_id is not None and str(thread_id) else None,
                        int(bool(is_own)),
                        str(topic) if topic else None,
                        str(skill) if skill else None,
                        int(time.time()),
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM message_context
                    WHERE rowid IN (
                        SELECT rowid FROM message_context
                        ORDER BY updated_at ASC
                        LIMIT MAX((SELECT COUNT(*) FROM message_context) - ?, 0)
                    )
                    """,
                    (_MAX_ENTRIES,),
                )
        except (OSError, sqlite3.Error):
            return


async def record_if_absent_async(*args: Any, **kwargs: Any) -> None:
    """Run the insert-if-absent operation away from the gateway event loop."""
    await asyncio.to_thread(record_if_absent, *args, **kwargs)


def lookup(chat_id: object, message_id: object) -> dict[str, Any] | None:
    """Return one indexed message, or ``None`` when it is unknown."""
    if chat_id is None or message_id is None:
        return None
    with _LOCK:
        try:
            with _connect(_store_path()) as connection:
                row = connection.execute(
                    """
                    SELECT text, thread_id, is_own, topic, skill, updated_at
                    FROM message_context
                    WHERE chat_id = ? AND message_id = ?
                    """,
                    (str(chat_id), str(message_id)),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
    if row is None:
        return None
    text, thread_id, is_own, topic, skill, updated_at = row
    result: dict[str, Any] = {
        "text": text,
        "is_own": bool(is_own),
        "ts": updated_at,
    }
    if thread_id:
        result["thread_id"] = thread_id
    if topic:
        result["topic"] = topic
    if skill:
        result["skill"] = skill
    return result


async def lookup_async(chat_id: object, message_id: object) -> dict[str, Any] | None:
    return await asyncio.to_thread(lookup, chat_id, message_id)
