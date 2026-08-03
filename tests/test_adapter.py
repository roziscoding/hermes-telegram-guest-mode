import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
for path in (PLUGIN_DIR,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gateway.platforms.base import SendResult
from plugins.platforms.telegram import adapter as telegram_base

from adapter import (
    GuestFinalizationDeferredError,
    GuestTelegramAdapter,
    guest_chat_id,
    is_guest_chat_id,
)


@pytest.fixture
def adapter():
    instance = object.__new__(GuestTelegramAdapter)
    instance._bot = SimpleNamespace(
        first_name="Test Bot",
        _post=AsyncMock(return_value={"inline_message_id": "guest-inline-1"}),
    )
    instance._guest_queries = {}
    return instance


def test_guest_chat_ids_are_namespaced_and_round_trip():
    chat_id = guest_chat_id("query-123")

    assert chat_id == "guest:query-123"
    assert is_guest_chat_id(chat_id)
    assert not is_guest_chat_id("16715013")


@pytest.mark.asyncio
async def test_guest_preview_is_buffered_without_calling_telegram(adapter):
    chat_id = guest_chat_id("query-123")
    adapter._remember_guest_query(chat_id, "query-123")

    result = await adapter.send(chat_id, "partial", metadata={"expect_edits": True})

    assert result.success is True
    assert result.message_id == chat_id
    assert adapter._guest_queries[chat_id].latest_content == "partial"
    adapter._bot._post.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_final_edit_defers_to_turn_final_send(adapter):
    chat_id = guest_chat_id("query-123")
    adapter._remember_guest_query(chat_id, "query-123")

    with pytest.raises(GuestFinalizationDeferredError):
        await adapter.edit_message(
            chat_id,
            chat_id,
            "final answer",
            finalize=True,
        )
    adapter._bot._post.assert_not_awaited()

    final = await adapter.send(
        chat_id,
        "actual final answer",
        metadata={"notify": True},
    )

    assert final.success is True
    adapter._bot._post.assert_awaited_once()
    method, data = adapter._bot._post.await_args.args
    assert method == "answerGuestQuery"
    assert data["guest_query_id"] == "query-123"
    assert data["result"].input_message_content.message_text == "actual final answer"


@pytest.mark.asyncio
async def test_transformed_final_edit_cannot_suppress_turn_final_send(adapter):
    chat_id = guest_chat_id("query-transformed")
    adapter._remember_guest_query(chat_id, "query-transformed")

    await adapter.send(
        chat_id,
        "streamed original",
        metadata={"expect_edits": True},
    )
    with pytest.raises(GuestFinalizationDeferredError):
        await adapter.edit_message(
            chat_id,
            chat_id,
            "streamed original",
            finalize=True,
        )
    with pytest.raises(GuestFinalizationDeferredError):
        await adapter.edit_message(
            chat_id,
            chat_id,
            "transformed final",
            finalize=True,
        )

    result = await adapter.send(
        chat_id,
        "transformed final",
        metadata={"notify": True},
    )

    assert result.success is True
    assert adapter._bot._post.await_count == 1
    _, data = adapter._bot._post.await_args.args
    assert data["result"].input_message_content.message_text == "transformed final"


@pytest.mark.asyncio
async def test_stream_segment_send_does_not_answer_guest_query(adapter):
    chat_id = guest_chat_id("query-segment")
    adapter._remember_guest_query(chat_id, "query-segment")

    preview = await adapter.send(
        chat_id,
        "pre-tool preamble",
        metadata={"notify": True, "expect_edits": True},
    )

    assert preview.success is True
    adapter._bot._post.assert_not_awaited()

    final = await adapter.send(
        chat_id,
        "post-tool final answer",
        metadata={"notify": True},
    )

    assert final.success is True
    assert adapter._bot._post.await_count == 1
    _, data = adapter._bot._post.await_args.args
    assert data["result"].input_message_content.message_text == "post-tool final answer"


@pytest.mark.asyncio
async def test_concurrent_final_sends_call_answer_guest_query_once(adapter):
    chat_id = guest_chat_id("query-race")
    adapter._remember_guest_query(chat_id, "query-race")

    results = await asyncio.gather(
        adapter.send(chat_id, "first", metadata={"notify": True}),
        adapter.send(chat_id, "second", metadata={"notify": True}),
    )

    assert all(result.success for result in results)
    assert adapter._bot._post.await_count == 1


@pytest.mark.asyncio
async def test_guest_final_send_answers_query_without_streaming(adapter):
    chat_id = guest_chat_id("query-456")
    adapter._remember_guest_query(chat_id, "query-456")

    result = await adapter.send(chat_id, "plain final", metadata={"notify": True})

    assert result.success is True
    adapter._bot._post.assert_awaited_once()
    _, data = adapter._bot._post.await_args.args
    assert data["guest_query_id"] == "query-456"
    assert data["result"].input_message_content.message_text == "plain final"


@pytest.mark.asyncio
async def test_guest_typing_is_a_noop(adapter):
    chat_id = guest_chat_id("query-123")

    assert await adapter.send_typing(chat_id) is None
    adapter._bot._post.assert_not_awaited()


def test_allowed_updates_include_guest_message_without_removing_existing_types():
    original = ("message", "callback_query")

    result = GuestTelegramAdapter.with_guest_update_type(original)

    assert result == ("message", "callback_query", "guest_message")
    assert GuestTelegramAdapter.with_guest_update_type(result) == result


@pytest.mark.asyncio
async def test_polling_update_type_patch_is_serialized_and_restored(monkeypatch):
    active = 0
    max_active = 0

    async def fake_start(self, app, **kwargs):
        nonlocal active, max_active
        assert "guest_message" in telegram_base.Update.ALL_TYPES
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return (1, asyncio.Event())

    monkeypatch.setattr(
        telegram_base.TelegramAdapter,
        "_start_polling_once",
        fake_start,
    )
    original = telegram_base.Update.ALL_TYPES
    adapters = [object.__new__(GuestTelegramAdapter) for _ in range(2)]
    for instance in adapters:
        instance._guest_handler_apps = set()
    apps = [SimpleNamespace(add_handler=lambda *args, **kwargs: None) for _ in range(2)]

    await asyncio.gather(
        adapters[0]._start_polling_once(apps[0]),
        adapters[1]._start_polling_once(apps[1]),
    )

    assert max_active == 1
    assert telegram_base.Update.ALL_TYPES is original


def test_guest_allowlist_supports_wildcard():
    config = SimpleNamespace(
        extra={"allow_from": ["*"]},
        home_channel=None,
    )

    assert GuestTelegramAdapter._configured_guest_user_ids(config) == {"*"}


def test_guest_authorization_uses_gateway_pairing_policy(adapter):
    calls = []
    adapter.config = SimpleNamespace(extra={}, home_channel=None)
    adapter._authorization_check = (
        lambda user_id, chat_type, chat_id: calls.append(
            (user_id, chat_type, chat_id)
        )
        or True
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42, username="roz", full_name="Roz")
    )

    assert adapter._guest_caller_authorized(message) is True
    assert calls == [("42", "dm", "42")]


def test_explicit_guest_allowlist_remains_authoritative(adapter, monkeypatch):
    adapter.config = SimpleNamespace(extra={"allow_from": ["7"]}, home_channel=None)
    adapter._authorization_check = lambda *args: True
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    message = SimpleNamespace(from_user=SimpleNamespace(id=42))

    assert adapter._guest_caller_authorized(message) is False


def test_guest_authorization_without_central_policy_fails_closed(adapter, monkeypatch):
    adapter.config = SimpleNamespace(extra={}, home_channel=None)
    adapter._authorization_check = None
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    message = SimpleNamespace(from_user=SimpleNamespace(id=42))

    assert adapter._guest_caller_authorized(message) is False


def test_guest_authorization_failure_denies_instead_of_using_environment(adapter, monkeypatch):
    adapter.config = SimpleNamespace(extra={}, home_channel=None)
    adapter._authorization_check = lambda *args: (_ for _ in ()).throw(
        RuntimeError("authorization backend unavailable")
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    message = SimpleNamespace(from_user=SimpleNamespace(id=42))

    assert adapter._guest_caller_authorized(message) is False


def test_application_assignment_installs_guest_handler_before_transport(adapter):
    calls = []
    app = SimpleNamespace(add_handler=lambda *args, **kwargs: calls.append((args, kwargs)))
    adapter._guest_handler_apps = set()

    adapter._app = app

    assert len(calls) == 1
    assert calls[0][1]["group"] == -100


def test_guest_handler_installation_is_fail_closed(adapter):
    broken_app = SimpleNamespace(add_handler=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    assert adapter._ensure_guest_handler(broken_app) is False


@pytest.mark.asyncio
async def test_guest_update_is_dispatched_on_synthetic_chat(adapter, monkeypatch):
    adapter.config = SimpleNamespace(extra={"allow_from": ["42"]})
    adapter._build_message_event = lambda message, message_type, update_id=None: SimpleNamespace(
        source=SimpleNamespace(chat_id="999", chat_type="group"),
        raw_message=message,
        text=message.text,
    )
    adapter.handle_message = AsyncMock()
    update = SimpleNamespace(
        update_id=77,
        api_kwargs={
            "guest_message": {
                "message_id": 9,
                "date": 0,
                "chat": {"id": 999, "type": "group", "title": "Guest group"},
                "from": {"id": 42, "is_bot": False, "first_name": "Roz"},
                "text": "@aster oi",
                "guest_query_id": "query-77",
            }
        },
    )

    await adapter._handle_guest_update(update, None)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == "guest:query-77"
    assert event.source.chat_type == "guest"
    assert event.text == "@aster oi"
    assert adapter._guest_queries["guest:query-77"].query_id == "query-77"


@pytest.mark.asyncio
async def test_unauthorized_guest_update_is_ignored(adapter):
    adapter.config = SimpleNamespace(extra={"allow_from": ["7"]})
    adapter.handle_message = AsyncMock()
    update = SimpleNamespace(
        update_id=78,
        api_kwargs={
            "guest_message": {
                "message_id": 10,
                "date": 0,
                "chat": {"id": 999, "type": "group"},
                "from": {"id": 42, "is_bot": False, "first_name": "Roz"},
                "text": "@aster oi",
                "guest_query_id": "query-78",
            }
        },
    )

    await adapter._handle_guest_update(update, None)

    adapter.handle_message.assert_not_awaited()
    assert adapter._guest_queries == {}


@pytest.mark.asyncio
async def test_normal_send_delegates_unchanged_to_bundled_adapter(adapter, monkeypatch):
    expected = SendResult(success=True, message_id="123")
    parent_send = AsyncMock(return_value=expected)
    monkeypatch.setattr(telegram_base.TelegramAdapter, "send", parent_send)

    result = await adapter.send("16715013", "normal", reply_to="8", metadata={"notify": True})

    assert result is expected
    parent_send.assert_awaited_once_with(
        "16715013", "normal", reply_to="8", metadata={"notify": True}
    )


@pytest.mark.asyncio
async def test_guest_result_uses_bot_name_and_truncates_long_text(adapter):
    chat_id = guest_chat_id("query-long")
    adapter._remember_guest_query(chat_id, "query-long")

    result = await adapter.send(chat_id, "x" * 5000, metadata={"notify": True})

    assert result.success is True
    _, data = adapter._bot._post.await_args.args
    article = data["result"]
    assert article.title == "Test Bot"
    assert telegram_base.utf16_len(article.input_message_content.message_text) <= 4096


def test_guest_query_state_is_bounded(adapter):
    adapter.MAX_RETAINED_QUERIES = 2
    adapter._remember_guest_query("guest:one", "one")
    adapter._remember_guest_query("guest:two", "two")
    adapter._remember_guest_query("guest:three", "three")

    assert set(adapter._guest_queries) == {"guest:two", "guest:three"}
