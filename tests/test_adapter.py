import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from telegram.error import BadRequest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
for path in (PLUGIN_DIR,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gateway.platforms.base import SendResult
from plugins.platforms.telegram import adapter as telegram_base

from adapter import (
    GuestEditError,
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
    instance._status_message_ids = {}
    return instance


def test_guest_chat_ids_are_namespaced_and_round_trip():
    chat_id = guest_chat_id("query-123")

    assert chat_id == "guest:query-123"
    assert is_guest_chat_id(chat_id)
    assert not is_guest_chat_id("16715013")


@pytest.mark.asyncio
async def test_guest_preview_answers_query_and_remembers_inline_message(adapter):
    chat_id = guest_chat_id("query-123")
    adapter._remember_guest_query(chat_id, "query-123")

    result = await adapter.send(chat_id, "partial", metadata={"expect_edits": True})

    assert result.success is True
    assert result.message_id == "guest-inline-1"
    assert adapter._guest_queries[chat_id].latest_content == "partial"
    assert adapter._guest_queries[chat_id].inline_message_id == "guest-inline-1"
    adapter._bot._post.assert_awaited_once()


@pytest.mark.asyncio
async def test_legitimate_short_final_is_not_mistaken_for_failed_edit_tail(adapter):
    chat_id = guest_chat_id("query-short-final")
    adapter._remember_guest_query(chat_id, "query-short-final")

    await adapter.send(chat_id, "Research answer", metadata={})
    await adapter.send(chat_id, "answer", metadata={"notify": True})

    assert adapter._guest_queries[chat_id].delivered_content == "answer"
    _, final_payload = adapter._bot._post.await_args.args
    assert final_payload["text"] == "answer"


@pytest.mark.asyncio
async def test_unmarked_first_commentary_is_replaced_by_marked_final_answer(adapter):
    chat_id = guest_chat_id("query-commentary")
    adapter._remember_guest_query(chat_id, "query-commentary")

    commentary = await adapter.send(chat_id, "Searching the web…", metadata={})
    final = await adapter.send(
        chat_id,
        "Guest Mode lets bots answer without joining the chat.",
        metadata={"notify": True},
    )

    assert commentary.message_id == "guest-inline-1"
    assert final.message_id == "guest-inline-1"
    assert [call.args[0] for call in adapter._bot._post.await_args_list] == [
        "answerGuestQuery",
        "editMessageText",
    ]
    assert adapter._guest_queries[chat_id].delivered_content == (
        "Guest Mode lets bots answer without joining the chat."
    )


@pytest.mark.asyncio
async def test_guest_preview_is_answered_then_final_edit_updates_same_message(adapter):
    chat_id = guest_chat_id("query-123")
    adapter._remember_guest_query(chat_id, "query-123")

    preview = await adapter.send(
        chat_id,
        "Searching the web…",
        metadata={"notify": True, "expect_edits": True},
    )
    final = await adapter.edit_message(
        chat_id,
        preview.message_id,
        "Guest Mode lets bots answer without joining the chat.",
        finalize=True,
    )

    assert preview.success is True
    assert preview.message_id == "guest-inline-1"
    assert final.success is True
    assert adapter._bot._post.await_count == 2
    answer_call, edit_call = adapter._bot._post.await_args_list
    assert answer_call.args[0] == "answerGuestQuery"
    assert edit_call.args == (
        "editMessageText",
        {
            "inline_message_id": "guest-inline-1",
            "text": "Guest Mode lets bots answer without joining the chat\\.",
            "parse_mode": telegram_base.ParseMode.MARKDOWN_V2,
        },
    )


@pytest.mark.asyncio
async def test_guest_final_edit_uses_telegram_markdown_v2(adapter, monkeypatch):
    chat_id = guest_chat_id("query-formatted-final")
    adapter._remember_guest_query(chat_id, "query-formatted-final")
    monkeypatch.setattr(
        adapter,
        "format_message",
        MagicMock(return_value="*Bold* and `code`"),
    )

    preview = await adapter.send(
        chat_id,
        "**Bold** and `code`",
        metadata={"expect_edits": True},
    )
    await adapter.edit_message(
        chat_id,
        preview.message_id,
        "**Bold** and `code`",
        finalize=True,
    )

    _, edit_payload = adapter._bot._post.await_args.args
    assert edit_payload == {
        "inline_message_id": "guest-inline-1",
        "text": "*Bold* and `code`",
        "parse_mode": telegram_base.ParseMode.MARKDOWN_V2,
    }


@pytest.mark.asyncio
async def test_guest_final_markdown_rejection_falls_back_to_plain_text(adapter):
    chat_id = guest_chat_id("query-format-fallback")
    adapter._remember_guest_query(chat_id, "query-format-fallback")
    adapter._bot._post.side_effect = [
        {"inline_message_id": "guest-inline-1"},
        BadRequest("can't parse entities"),
        True,
    ]

    preview = await adapter.send(
        chat_id,
        "Preview",
        metadata={"expect_edits": True},
    )
    final = await adapter.edit_message(
        chat_id,
        preview.message_id,
        "**Bold** final",
        finalize=True,
    )

    assert final.success is True
    assert adapter._bot._post.await_args.args == (
        "editMessageText",
        {
            "inline_message_id": "guest-inline-1",
            "text": "**Bold** final",
        },
    )


@pytest.mark.asyncio
async def test_unchanged_guest_final_treats_not_modified_as_success(adapter):
    chat_id = guest_chat_id("query-not-modified")
    adapter._remember_guest_query(chat_id, "query-not-modified")
    adapter._bot._post.side_effect = [
        {"inline_message_id": "guest-inline-1"},
        BadRequest("Message is not modified"),
    ]

    preview = await adapter.send(
        chat_id,
        "Already final",
        metadata={"expect_edits": True},
    )
    final = await adapter.edit_message(
        chat_id,
        preview.message_id,
        "Already final",
        finalize=True,
    )

    assert final.success is True
    assert adapter._bot._post.await_count == 2


@pytest.mark.asyncio
async def test_plain_fallback_marks_identical_guest_final_complete(adapter):
    chat_id = guest_chat_id("query-fallback-complete")
    adapter._remember_guest_query(chat_id, "query-fallback-complete")
    adapter._bot._post.side_effect = [
        {"inline_message_id": "guest-inline-1"},
        BadRequest("can't parse entities"),
        True,
    ]

    preview = await adapter.send(
        chat_id,
        "Preview",
        metadata={"expect_edits": True},
    )
    first_final = await adapter.edit_message(
        chat_id,
        preview.message_id,
        "**Bold** final",
        finalize=True,
    )
    repeated_final = await adapter.send(
        chat_id,
        "**Bold** final",
        metadata={"notify": True},
    )

    assert first_final.success is True
    assert repeated_final.success is True
    assert adapter._bot._post.await_count == 3


@pytest.mark.asyncio
async def test_guest_final_edit_is_first_delivery_when_no_preview_exists(adapter):
    chat_id = guest_chat_id("query-123")
    adapter._remember_guest_query(chat_id, "query-123")

    final = await adapter.edit_message(
        chat_id,
        chat_id,
        "actual final answer",
        finalize=True,
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
    await adapter.edit_message(
        chat_id,
        "guest-inline-1",
        "streamed original",
        finalize=True,
    )
    await adapter.edit_message(
        chat_id,
        "guest-inline-1",
        "transformed final",
        finalize=True,
    )

    result = await adapter.send(
        chat_id,
        "transformed final",
        metadata={"notify": True},
    )

    assert result.success is True
    assert adapter._bot._post.await_count == 3
    answer_call, _, edit_call = adapter._bot._post.await_args_list
    assert answer_call.args[0] == "answerGuestQuery"
    assert edit_call.args == (
        "editMessageText",
        {
            "inline_message_id": "guest-inline-1",
            "text": "transformed final",
            "parse_mode": telegram_base.ParseMode.MARKDOWN_V2,
        },
    )


@pytest.mark.asyncio
async def test_post_tool_segment_edits_the_existing_guest_response(adapter):
    chat_id = guest_chat_id("query-segment")
    adapter._remember_guest_query(chat_id, "query-segment")

    preview = await adapter.send(
        chat_id,
        "pre-tool preamble",
        metadata={"notify": True, "expect_edits": True},
    )

    assert preview.success is True
    assert preview.message_id == "guest-inline-1"

    final = await adapter.send(
        chat_id,
        "post-tool final answer",
        metadata={"notify": True},
    )

    assert final.success is True
    assert adapter._bot._post.await_count == 2
    answer_call, edit_call = adapter._bot._post.await_args_list
    assert answer_call.args[0] == "answerGuestQuery"
    assert edit_call.args == (
        "editMessageText",
        {
            "inline_message_id": "guest-inline-1",
            "text": "post\\-tool final answer",
            "parse_mode": telegram_base.ParseMode.MARKDOWN_V2,
        },
    )


@pytest.mark.asyncio
async def test_concurrent_final_sends_call_answer_guest_query_once(adapter):
    chat_id = guest_chat_id("query-race")
    adapter._remember_guest_query(chat_id, "query-race")

    results = await asyncio.gather(
        adapter.send(chat_id, "first", metadata={"notify": True}),
        adapter.send(chat_id, "second", metadata={"notify": True}),
    )

    assert all(result.success for result in results)
    methods = [call.args[0] for call in adapter._bot._post.await_args_list]
    assert methods.count("answerGuestQuery") == 1
    assert methods.count("editMessageText") == 1


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
async def test_failed_guest_edit_can_retry_without_losing_final_content(adapter):
    chat_id = guest_chat_id("query-retry")
    adapter._remember_guest_query(chat_id, "query-retry")
    adapter._bot._post.side_effect = [
        {"inline_message_id": "guest-inline-1"},
        RuntimeError("temporary edit failure"),
        True,
    ]

    await adapter.send(chat_id, "Searching…", metadata={"expect_edits": True})
    with pytest.raises(GuestEditError):
        await adapter.edit_message(
            chat_id,
            "guest-inline-1",
            "Final answer",
            finalize=True,
        )
    retried = await adapter.send(
        chat_id,
        "answer",
        metadata={"notify": True},
    )

    assert retried.success is True
    assert adapter._guest_queries[chat_id].delivered_content == "Final answer"
    assert [call.args[0] for call in adapter._bot._post.await_args_list] == [
        "answerGuestQuery",
        "editMessageText",
        "editMessageText",
    ]


@pytest.mark.asyncio
async def test_cancelled_concurrent_edit_cannot_corrupt_pending_full_retry(adapter):
    chat_id = guest_chat_id("query-concurrent-cancel")
    adapter._remember_guest_query(chat_id, "query-concurrent-cancel")
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()
    calls = 0

    async def post(method, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"inline_message_id": "guest-inline-1"}
        if calls == 2:
            edit_started.set()
            await release_edit.wait()
            raise RuntimeError("temporary edit failure")
        return True

    adapter._bot._post.side_effect = post
    await adapter.send(chat_id, "Prefix", metadata={"expect_edits": True})

    failing = asyncio.create_task(
        adapter.edit_message(
            chat_id,
            "guest-inline-1",
            "Prefix and tail",
            finalize=True,
        )
    )
    await edit_started.wait()
    cancelled = asyncio.create_task(
        adapter.edit_message(
            chat_id,
            "guest-inline-1",
            "Cancelled replacement",
            finalize=True,
        )
    )
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    release_edit.set()
    with pytest.raises(GuestEditError):
        await failing

    retried = await adapter.send(
        chat_id,
        "and tail",
        metadata={"notify": True},
    )

    assert retried.success is True
    assert adapter._guest_queries[chat_id].delivered_content == "Prefix and tail"
    _, final_payload = adapter._bot._post.await_args.args
    assert final_payload["text"] == "Prefix and tail"


@pytest.mark.asyncio
async def test_answer_guest_query_transport_failure_is_never_retried(adapter):
    chat_id = guest_chat_id("query-answer-timeout")
    adapter._remember_guest_query(chat_id, "query-answer-timeout")
    adapter._bot._post.side_effect = RuntimeError("Timed out")

    first = await adapter.send(chat_id, "Answer", metadata={"notify": True})
    second = await adapter.send(chat_id, "Answer", metadata={"notify": True})

    assert first.success is False
    assert first.retryable is False
    assert second.success is False
    assert second.retryable is False
    adapter._bot._post.assert_awaited_once()


def test_guest_stream_limit_prevents_stream_consumer_chunk_replacement(
    adapter, monkeypatch
):
    parent_limit = MagicMock(return_value=4096)
    monkeypatch.setattr(
        telegram_base.TelegramAdapter,
        "max_message_length_for_chat",
        parent_limit,
    )

    assert adapter.max_message_length_for_chat("guest:query-long") > 1_000_000
    assert adapter.max_message_length_for_chat("16715013") == 4096
    parent_limit.assert_called_once_with("16715013")


@pytest.mark.asyncio
async def test_stream_consumer_edit_failure_retries_complete_replacement(adapter):
    chat_id = guest_chat_id("query-stream-fallback")
    adapter._remember_guest_query(chat_id, "query-stream-fallback")
    adapter._bot._post.side_effect = [
        {"inline_message_id": "guest-inline-1"},
        RuntimeError("temporary edit failure"),
        True,
    ]
    consumer = GatewayStreamConsumer(
        adapter,
        chat_id,
        StreamConsumerConfig(cursor="", transport="edit"),
    )

    assert await consumer._send_or_edit("Prefix") is True
    assert not await consumer._send_or_edit("Prefix and tail")
    assert consumer._fallback_final_send is False

    retried = await adapter.send(
        chat_id,
        "and tail",
        metadata={"notify": True},
    )

    assert retried.success is True
    assert adapter._guest_queries[chat_id].delivered_content == "Prefix and tail"
    _, final_payload = adapter._bot._post.await_args.args
    assert final_payload["text"] == "Prefix and tail"


@pytest.mark.asyncio
async def test_missing_inline_message_id_raises_instead_of_claiming_edit_delivery(adapter):
    chat_id = guest_chat_id("query-missing-inline")
    adapter._remember_guest_query(chat_id, "query-missing-inline")
    adapter._bot._post.return_value = {}

    initial = await adapter.send(chat_id, "Preview", metadata={"expect_edits": True})

    assert initial.success is True
    with pytest.raises(GuestEditError):
        await adapter.edit_message(
            chat_id,
            chat_id,
            "Final answer",
            finalize=True,
        )
    adapter._bot._post.assert_awaited_once()


@pytest.mark.asyncio
async def test_transformed_edit_failure_raises_for_gateway_reconciliation(adapter):
    chat_id = guest_chat_id("query-transformed-failure")
    adapter._remember_guest_query(chat_id, "query-transformed-failure")
    adapter._bot._post.side_effect = [
        {"inline_message_id": "guest-inline-1"},
        RuntimeError("temporary edit failure"),
    ]

    await adapter.send(chat_id, "Preview", metadata={"expect_edits": True})

    with pytest.raises(GuestEditError):
        await adapter.edit_message(
            chat_id,
            "guest-inline-1",
            "Plugin-transformed final",
            finalize=True,
        )

    assert adapter._guest_queries[chat_id].delivered_content == "Preview"
    assert adapter._guest_queries[chat_id].latest_content == "Plugin-transformed final"


@pytest.mark.asyncio
async def test_unmarked_auxiliary_queued_during_initial_answer_is_suppressed(adapter):
    chat_id = guest_chat_id("query-inflight-footer")
    adapter._remember_guest_query(chat_id, "query-inflight-footer")
    answer_started = asyncio.Event()
    release_answer = asyncio.Event()

    async def post(method, data):
        answer_started.set()
        await release_answer.wait()
        return {"inline_message_id": "guest-inline-1"}

    adapter._bot._post.side_effect = post
    initial = asyncio.create_task(adapter.send(chat_id, "Initial commentary", metadata={}))
    await answer_started.wait()
    auxiliary = asyncio.create_task(adapter.send(chat_id, "Model: gpt-test", metadata={}))
    await asyncio.sleep(0)
    release_answer.set()

    initial_result, auxiliary_result = await asyncio.gather(initial, auxiliary)

    assert initial_result.success is True
    assert auxiliary_result.success is True
    assert adapter._guest_queries[chat_id].delivered_content == "Initial commentary"
    assert adapter._guest_queries[chat_id].latest_content == "Initial commentary"
    adapter._bot._post.assert_awaited_once()


@pytest.mark.asyncio
async def test_auxiliary_send_cannot_replace_completed_guest_answer(adapter):
    chat_id = guest_chat_id("query-footer")
    adapter._remember_guest_query(chat_id, "query-footer")

    await adapter.send(chat_id, "Final answer", metadata={"notify": True})
    footer = await adapter.send(chat_id, "Model: gpt-test", metadata={})

    assert footer.success is True
    assert footer.message_id == "guest-inline-1"
    assert adapter._guest_queries[chat_id].delivered_content == "Final answer"
    assert adapter._guest_queries[chat_id].latest_content == "Final answer"
    adapter._bot._post.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_status_updates_never_consume_or_replace_guest_response(adapter):
    chat_id = guest_chat_id("query-status")
    adapter._remember_guest_query(chat_id, "query-status")

    first = await adapter.send_or_update_status(chat_id, "tool", "Searching…")
    second = await adapter.send_or_update_status(chat_id, "tool", "Search complete")

    assert first.success is True
    assert second.success is True
    assert first.message_id is None
    assert second.message_id is None
    assert adapter._guest_queries[chat_id].latest_content == ""
    adapter._bot._post.assert_not_awaited()


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
async def test_normal_status_updates_delegate_to_bundled_adapter(adapter, monkeypatch):
    expected = SendResult(success=True, message_id="status-1")
    parent_status = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        telegram_base.TelegramAdapter,
        "send_or_update_status",
        parent_status,
    )

    result = await adapter.send_or_update_status(
        "16715013",
        "tool",
        "Searching…",
        metadata={"thread_id": "8"},
    )

    assert result is expected
    parent_status.assert_awaited_once_with(
        "16715013",
        "tool",
        "Searching…",
        metadata={"thread_id": "8"},
    )


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
