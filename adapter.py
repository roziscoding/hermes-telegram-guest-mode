from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from gateway.platforms.base import SendResult
from plugins.platforms.telegram import adapter as telegram_base
from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    MessageEntity,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, TypeHandler

GUEST_UPDATE_TYPE = "guest_message"
GUEST_CHAT_PREFIX = "guest:"
GUEST_THINKING_TEXT = "✨ Thinking"
GUEST_THINKING_CUSTOM_EMOJI_ID = "5463297803235113601"

logger = logging.getLogger(__name__)


class GuestEditError(RuntimeError):
    """A Guest edit failed and the gateway must keep the final response eligible."""


def guest_chat_id(query_id: str) -> str:
    return f"{GUEST_CHAT_PREFIX}{query_id}"


def is_guest_chat_id(chat_id: object) -> bool:
    return str(chat_id).startswith(GUEST_CHAT_PREFIX)


@dataclass
class GuestQueryState:
    query_id: str
    latest_content: str = ""
    delivered_content: str = ""
    delivered_finalized: bool = False
    delivered_rich_finalized: bool = False
    rich_edit_ambiguous: bool = False
    rich_rejected_content: str = ""
    answer_attempted: bool = False
    answer_error: str | None = None
    answered: bool = False
    inline_message_id: str | None = None
    placeholder_active: bool = False
    answer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class GuestTelegramAdapter(telegram_base.TelegramAdapter):
    """Thin native Guest Mode layer over Hermes' bundled Telegram adapter."""

    MAX_RETAINED_QUERIES = 1024
    ANSWER_ATTEMPT_RETENTION_SECONDS = 24 * 60 * 60
    GUEST_STREAM_CONSUMER_LIMIT = 2_147_483_647
    GUEST_CONTENT_ENRICHERS = (
        (
            ("photo", "video", "audio", "voice", "document"),
            "_enrich_guest_direct_media",
            "_enrich_guest_reply_media",
        ),
        (("location", "venue"), "_enrich_guest_location", "_enrich_guest_location"),
        (("sticker",), "_enrich_guest_sticker", "_enrich_guest_sticker"),
    )

    def __init__(self, config):
        super().__init__(config)
        self._guest_queries: dict[str, GuestQueryState] = {}
        self._answer_attempted_query_ids: dict[str, float] = {}
        self._guest_handler_apps: set[int] = set()

    def _remember_guest_query(self, chat_id: str, query_id: str) -> GuestQueryState:
        now = time.monotonic()
        while self._answer_attempted_query_ids:
            retained_id = next(iter(self._answer_attempted_query_ids))
            if self._answer_attempted_query_ids[retained_id] > now:
                break
            self._answer_attempted_query_ids.pop(retained_id, None)
        state = self._guest_queries.get(chat_id)
        if state is None:
            if len(self._guest_queries) >= self.MAX_RETAINED_QUERIES:
                self._guest_queries.pop(next(iter(self._guest_queries)))
            state = GuestQueryState(
                query_id=query_id,
                answer_attempted=query_id in self._answer_attempted_query_ids,
            )
            self._guest_queries[chat_id] = state
        return state

    def _ensure_guest_handler(self, app) -> bool:
        """Install the catch-all guest handler without risking normal Telegram."""
        if GUEST_UPDATE_TYPE not in Update.ALL_TYPES:
            logger.error(
                "[Telegram Guest] python-telegram-bot 22.8+ is required "
                "for guest_message"
            )
            return False
        installed = getattr(self, "_guest_handler_apps", None)
        if installed is None:
            installed = self._guest_handler_apps = set()
        app_id = id(app)
        if app_id in installed:
            return True
        try:
            app.add_handler(TypeHandler(Update, self._handle_guest_update_and_stop), group=-100)
        except Exception:
            logger.warning(
                "[Telegram Guest] Could not install guest handler; normal Telegram remains active",
                exc_info=True,
            )
            return False
        installed.add(app_id)
        return True

    def _register_handlers(self, app) -> None:
        """Keep Guest wiring on PTB applications rebuilt by older Hermes cores."""
        self._ensure_guest_handler(app)
        super()._register_handlers(app)

    @staticmethod
    def _configured_guest_ids(config, key: str) -> set[str]:
        values: list[Any] = []
        extra = getattr(config, "extra", {}) or {}
        configured = extra.get(key)
        if isinstance(configured, (list, tuple, set)):
            values.extend(configured)
        elif configured:
            values.extend(str(configured).split(","))

        return {str(value).strip() for value in values if str(value).strip()}

    @classmethod
    def _configured_guest_user_ids(cls, config) -> set[str]:
        return cls._configured_guest_ids(config, "allow_from")

    def _guest_caller_authorized(self, message: Message) -> bool:
        sender_chat = getattr(message, "sender_chat", None)
        sender_chat_id = str(getattr(sender_chat, "id", "") or "").strip()
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", "") or "").strip()

        if sender_chat_id:
            caller_id = sender_chat_id
            chat_type = "group"
            chat = getattr(message, "chat", None)
            chat_id = str(getattr(chat, "id", "") or caller_id).strip()
            allowlist_key = "group_allow_from"
        elif user_id:
            caller_id = user_id
            chat_type = "dm"
            chat_id = user_id
            allowlist_key = "allow_from"
        else:
            return False

        # Explicit scope-specific policy wins. Channel-profile Guest messages
        # carry Telegram's fake Channel_Bot in from_user, so authorize the real
        # sender_chat against group_allow_from when configured; otherwise fall
        # back to the global allow_from list shared by every Telegram sender.
        extra = getattr(self.config, "extra", {}) or {}
        configured_key = allowlist_key
        if sender_chat_id and extra.get("group_allow_from") is None:
            configured_key = "allow_from"
        if extra.get(configured_key) is not None:
            allowed = self._configured_guest_ids(self.config, configured_key)
            return caller_id in allowed or "*" in allowed

        # Use the profile-scoped authorization callback injected by GatewayRunner.
        auth_check = getattr(self, "_authorization_check", None)
        if callable(auth_check):
            try:
                return bool(auth_check(caller_id, chat_type, chat_id))
            except Exception:
                logger.warning(
                    "[Telegram Guest] Central caller authorization failed; denying request",
                    exc_info=True,
                )
                return False

        return False

    async def _enrich_guest_reply_media(
        self,
        message: Message,
        replied: Message,
        event: Any,
    ) -> None:
        """Reuse Hermes' generic replied-media cache for Guest messages."""
        media_count_before = len(event.media_urls)
        await self._cache_replied_media(message, event)
        if (
            getattr(replied, "voice", None) is not None
            and media_count_before == 0
            and len(event.media_urls) == 1
            and len(event.media_types) == 1
            and event.media_types[0].startswith("audio/")
        ):
            # The bundled generic cache classifies all audio as AUDIO. Preserve
            # a standalone voice note as VOICE so the gateway sends it through
            # STT. Mixed events keep their primary type; the per-item audio MIME
            # still routes only the voice attachment through transcription.
            event.message_type = telegram_base.MessageType.VOICE

        if (
            event.message_type == telegram_base.MessageType.VOICE
            and any(
                not media_type.startswith("audio/")
                for media_type in event.media_types
            )
        ):
            # VOICE is a whole-event fallback in Hermes' STT router. Once an
            # event mixes audio with another MIME, use TEXT as the neutral type
            # so each attachment follows its own MIME-specific pipeline.
            event.message_type = telegram_base.MessageType.TEXT

    async def _enrich_guest_direct_media(
        self,
        message: Message,
        content: Message,
        event: Any,
    ) -> None:
        """Cache media carried directly by the Guest query."""
        await self._cache_observed_media(content, event)
        if getattr(content, "voice", None) is not None and event.media_urls:
            event.message_type = telegram_base.MessageType.VOICE

    async def _enrich_guest_location(
        self,
        message: Message,
        content: Message,
        event: Any,
    ) -> None:
        """Append a direct or quoted location as agent-readable context."""
        venue = getattr(content, "venue", None)
        location = (
            getattr(venue, "location", None)
            if venue is not None
            else getattr(content, "location", None)
        )
        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            return

        label = "Replied-to Telegram location pin" if content is not message else "Telegram location pin"
        parts = [f"[{label}]"]
        title = getattr(venue, "title", None) if venue is not None else None
        address = getattr(venue, "address", None) if venue is not None else None
        if title:
            parts.append(f"Venue: {title}")
        if address:
            parts.append(f"Address: {address}")
        parts.extend(
            (
                f"latitude: {lat}",
                f"longitude: {lon}",
                f"Map: https://www.google.com/maps/search/?api=1&query={lat},{lon}",
            )
        )
        event.text = self._append_observed_note(event.text, "\n".join(parts))
        event.message_type = telegram_base.MessageType.LOCATION

    async def _enrich_guest_sticker(
        self,
        message: Message,
        content: Message,
        event: Any,
    ) -> None:
        """Describe a direct or quoted sticker while preserving prompt text."""
        prompt = event.text
        await self._handle_sticker(content, event)
        event.text = self._append_observed_note(prompt, event.text)
        event.message_type = telegram_base.MessageType.STICKER

    @classmethod
    def _guest_content_enricher(cls, content: Message, handler_index: int) -> str | None:
        for fields, direct_handler, reply_handler in cls.GUEST_CONTENT_ENRICHERS:
            if any(getattr(content, field, None) is not None for field in fields):
                return (direct_handler, reply_handler)[handler_index]
        return None

    async def _enrich_guest_direct(self, message: Message, event: Any) -> None:
        handler_name = self._guest_content_enricher(message, 0)
        if handler_name is not None:
            await getattr(self, handler_name)(message, message, event)

    async def _enrich_guest_reply(self, message: Message, event: Any) -> None:
        """Dispatch quoted Guest content through the matching Hermes enricher."""
        replied = getattr(message, "reply_to_message", None)
        if replied is None:
            return

        handler_name = self._guest_content_enricher(replied, 1)
        if handler_name is not None:
            await getattr(self, handler_name)(message, replied, event)

    async def _handle_guest_update(self, update: Update, context) -> None:
        raw = (getattr(update, "api_kwargs", None) or {}).get(GUEST_UPDATE_TYPE)
        message = getattr(update, "guest_message", None)
        if message is not None:
            query_id = str(getattr(message, "guest_query_id", "") or "").strip()
        elif isinstance(raw, dict):
            query_id = str(raw.get("guest_query_id") or "").strip()
            try:
                message = Message.de_json(raw, self._bot)
            except Exception:
                logger.warning("[Telegram Guest] Invalid guest_message payload", exc_info=True)
                return
        else:
            return
        if not query_id:
            return
        if not message:
            return
        prompt = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if not prompt and self._guest_content_enricher(message, 0) is None:
            return
        if not self._guest_caller_authorized(message):
            sender_chat = getattr(message, "sender_chat", None)
            caller = sender_chat or getattr(message, "from_user", None)
            logger.warning(
                "[Telegram Guest] Blocked unauthorized caller %s",
                getattr(caller, "id", None),
            )
            return

        synthetic_chat_id = guest_chat_id(query_id)
        state = self._remember_guest_query(synthetic_chat_id, query_id)
        if state.answer_attempted:
            logger.info(
                "[Telegram Guest] Ignoring duplicate query=%s",
                hashlib.sha256(query_id.encode("utf-8")).hexdigest()[:8],
            )
            return
        state.placeholder_active = True
        thinking = await self._publish_guest(synthetic_chat_id, GUEST_THINKING_TEXT)
        if not thinking.success:
            state.placeholder_active = False
            logger.warning(
                "[Telegram Guest] Could not publish initial thinking response query=%s: %s",
                hashlib.sha256(query_id.encode("utf-8")).hexdigest()[:8],
                thinking.error or "unknown error",
            )
            return

        event = self._build_message_event(
            message,
            telegram_base.MessageType.TEXT,
            update_id=getattr(update, "update_id", None),
        )
        event.text = prompt
        event.source.chat_id = synthetic_chat_id
        event.source.chat_type = "guest"
        await self._enrich_guest_direct(message, event)
        await self._enrich_guest_reply(message, event)
        await self.handle_message(event)

    async def _handle_guest_update_and_stop(self, update: Update, context) -> None:
        """Handle Guest updates exclusively so normal message handlers cannot double-process them."""
        raw = (getattr(update, "api_kwargs", None) or {}).get(GUEST_UPDATE_TYPE)
        if getattr(update, "guest_message", None) is None and not isinstance(raw, dict):
            return
        await self._handle_guest_update(update, context)
        raise ApplicationHandlerStop


    async def _try_edit_guest_rich(
        self,
        state: GuestQueryState,
        content: str,
        query_ref: str,
    ) -> bool:
        """Finalize an existing Guest inline response as Bot API rich content.

        ``True`` means the rich edit succeeded (or Telegram confirmed it was
        already identical). ``False`` means a permanent rejection made a
        MarkdownV2 fallback safe. Ambiguous/transient failures raise so callers
        never issue a second edit after the rich request may have succeeded.
        """
        try:
            await self._bot.do_api_request(
                "editMessageText",
                api_kwargs={
                    "inline_message_id": state.inline_message_id,
                    "rich_message": self._rich_message_payload(content),
                },
            )
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return True
            if self._is_rich_fallback_error(exc):
                if self._is_rich_capability_error(exc):
                    self._rich_send_disabled = True
                logger.info(
                    "[Telegram Guest] Rich final rejected for query=%s; "
                    "falling back to MarkdownV2: %s",
                    query_ref,
                    telegram_base._redact_telegram_error_text(exc),
                )
                return False
            raise GuestEditError(
                telegram_base._redact_telegram_error_text(exc)
            ) from exc
        logger.info(
            "[Telegram Guest] Rich-finalized response query=%s chars=%d",
            query_ref,
            len(content),
        )
        return True

    async def _publish_guest(
        self,
        chat_id: str,
        content: str,
        *,
        allow_after_answer: bool = True,
        finalize: bool = False,
        turn_final: bool = False,
    ) -> SendResult:
        state = self._guest_queries.get(chat_id)
        if state is None:
            return SendResult(success=False, error="Unknown guest query")
        query_ref = hashlib.sha256(state.query_id.encode("utf-8")).hexdigest()[:8]
        async with state.answer_lock:
            if state.answered and not allow_after_answer and not state.placeholder_active:
                logger.info(
                    "[Telegram Guest] Suppressed unmarked send after answer query=%s",
                    query_ref,
                )
                return SendResult(
                    success=True,
                    message_id=state.inline_message_id or chat_id,
                )
            if not self._bot:
                return SendResult(success=False, error="Not connected")

            text = (content or state.latest_content or "").strip()
            if (
                state.answered
                and state.latest_content
                and state.latest_content != state.delivered_content
                and text != state.latest_content
                and state.latest_content.endswith(text)
            ):
                # Hermes fallback sends only an un-delivered suffix because most
                # platforms can append a fresh message. Guest Bots own one inline
                # response, so replacing it with that suffix would truncate the
                # visible answer. Retry the complete pending replacement instead.
                text = state.latest_content
            if not text:
                return SendResult(success=False, error="Empty guest response")
            if state.answered and state.rich_edit_ambiguous:
                return SendResult(
                    success=True,
                    message_id=state.inline_message_id or chat_id,
                )
            rich_text = text
            rich_rejected = state.rich_rejected_content == rich_text
            rich_eligible = bool(
                state.answered
                and finalize
                and turn_final
                and not rich_rejected
                and self._rich_eligible(rich_text)
            )
            if (
                state.answered
                and state.delivered_finalized
                and rich_text == state.delivered_content
                and (state.delivered_rich_finalized or not rich_eligible)
            ):
                return SendResult(
                    success=True,
                    message_id=state.inline_message_id or chat_id,
                )
            if not rich_eligible and telegram_base.utf16_len(text) > self.MAX_MESSAGE_LENGTH:
                text = self.truncate_message(
                    text, self.MAX_MESSAGE_LENGTH, len_fn=telegram_base.utf16_len
                )[0]
            state.latest_content = text

            if state.answered:
                if not state.inline_message_id:
                    raise GuestEditError(
                        "Guest response cannot be edited: missing inline_message_id"
                    )
                if rich_eligible:
                    # Consume the one safe rich attempt before awaiting I/O. If
                    # cancellation or a transient error makes the outcome
                    # ambiguous, later gateway reconciliation must not edit the
                    # same inline message again.
                    state.rich_edit_ambiguous = True
                    rich_success = await self._try_edit_guest_rich(
                        state, rich_text, query_ref
                    )
                    state.rich_edit_ambiguous = False
                    if rich_success:
                        state.rich_rejected_content = ""
                        state.delivered_content = rich_text
                        state.delivered_finalized = True
                        state.delivered_rich_finalized = True
                        state.placeholder_active = False
                        return SendResult(
                            success=True,
                            message_id=state.inline_message_id,
                        )
                    rich_eligible = False
                    state.rich_rejected_content = rich_text
                    if telegram_base.utf16_len(rich_text) > self.MAX_MESSAGE_LENGTH:
                        text = self.truncate_message(
                            rich_text,
                            self.MAX_MESSAGE_LENGTH,
                            len_fn=telegram_base.utf16_len,
                        )[0]
                        state.latest_content = text
                if text == state.delivered_content and (
                    not finalize or state.delivered_finalized
                ):
                    state.placeholder_active = False
                    return SendResult(
                        success=True,
                        message_id=state.inline_message_id,
                    )
                try:
                    logger.info(
                        "[Telegram Guest] Editing response query=%s chars=%d",
                        query_ref,
                        len(text),
                    )
                    payload = {
                        "inline_message_id": state.inline_message_id,
                        "text": text,
                    }
                    if finalize:
                        payload["text"] = self.format_message(text)
                        payload["parse_mode"] = telegram_base.ParseMode.MARKDOWN_V2
                    try:
                        raw_response = await self._bot._post(
                            "editMessageText",
                            payload,
                        )
                        state.delivered_finalized = finalize
                        state.delivered_rich_finalized = False
                    except Exception as format_exc:
                        if not finalize or not isinstance(format_exc, BadRequest):
                            raise
                        if "not modified" in str(format_exc).lower():
                            raw_response = True
                            state.delivered_finalized = True
                            state.delivered_rich_finalized = False
                        else:
                            logger.warning(
                                "[Telegram Guest] MarkdownV2 edit failed for query=%s; "
                                "falling back to plain text: %s",
                                query_ref,
                                telegram_base._redact_telegram_error_text(format_exc),
                            )
                            try:
                                raw_response = await self._bot._post(
                                    "editMessageText",
                                    {
                                        "inline_message_id": state.inline_message_id,
                                        "text": text,
                                    },
                                )
                            except BadRequest as plain_exc:
                                if "not modified" not in str(plain_exc).lower():
                                    raise
                                raw_response = True
                            state.delivered_finalized = True
                            state.delivered_rich_finalized = False
                except Exception as exc:
                    raise GuestEditError(
                        telegram_base._redact_telegram_error_text(exc)
                    ) from exc
                state.delivered_content = text
                state.placeholder_active = False
                logger.info(
                    "[Telegram Guest] Edited response query=%s result_type=%s",
                    query_ref,
                    type(raw_response).__name__,
                )
                return SendResult(
                    success=True,
                    message_id=state.inline_message_id,
                    raw_response=raw_response,
                )

            if state.answer_attempted:
                return SendResult(
                    success=False,
                    error=state.answer_error or "Guest response was already attempted",
                    retryable=False,
                )
            state.answer_attempted = True
            self._answer_attempted_query_ids[state.query_id] = (
                time.monotonic() + self.ANSWER_ATTEMPT_RETENTION_SECONDS
            )
            result_id = hashlib.sha256(state.query_id.encode("utf-8")).hexdigest()[:32]
            result = InlineQueryResultArticle(
                id=result_id,
                title=getattr(self._bot, "first_name", None) or "Hermes",
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    entities=(
                        MessageEntity(
                            type=MessageEntity.CUSTOM_EMOJI,
                            offset=0,
                            length=1,
                            custom_emoji_id=GUEST_THINKING_CUSTOM_EMOJI_ID,
                        ),
                    )
                    if state.placeholder_active and text == GUEST_THINKING_TEXT
                    else (),
                ),
            )
            try:
                raw_response = await self._bot._post(
                    "answerGuestQuery",
                    {"guest_query_id": state.query_id, "result": result},
                )
            except Exception as exc:  # noqa: BLE001 - isolate transport failures
                state.answer_error = telegram_base._redact_telegram_error_text(exc)
                return SendResult(
                    success=False,
                    error=state.answer_error,
                    retryable=False,
                )

            state.answered = True
            inline_message_id = None
            if isinstance(raw_response, dict):
                inline_message_id = raw_response.get("inline_message_id")
            if inline_message_id is None:
                inline_message_id = getattr(raw_response, "inline_message_id", None)
            state.inline_message_id = str(inline_message_id) if inline_message_id else None
            state.delivered_content = text
            state.delivered_finalized = False
            state.delivered_rich_finalized = False
            logger.info(
                "[Telegram Guest] Answered query=%s chars=%d inline_id=%s result_type=%s",
                query_ref,
                len(text),
                bool(state.inline_message_id),
                type(raw_response).__name__,
            )
            return SendResult(
                success=True,
                message_id=state.inline_message_id or chat_id,
                raw_response=raw_response,
            )

    def max_message_length_for_chat(self, chat_id: str) -> int:
        if is_guest_chat_id(chat_id):
            # Prevent StreamConsumer from splitting one replaceable Guest bubble
            # into append-only chunks. _publish_guest enforces Telegram's actual
            # 4096 UTF-16-unit cap before each answer/edit request.
            return self.GUEST_STREAM_CONSUMER_LIMIT
        return super().max_message_length_for_chat(chat_id)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not is_guest_chat_id(chat_id):
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

        state = self._guest_queries.get(str(chat_id))
        if state is None:
            return SendResult(success=False, error="Unknown guest query")
        marked_visible = bool(
            metadata and (metadata.get("expect_edits") or metadata.get("notify"))
        )
        # The answered check happens under answer_lock so an auxiliary send queued
        # behind the initial response cannot become an accidental second frame.
        return await self._publish_guest(
            str(chat_id),
            content,
            allow_after_answer=marked_visible,
            finalize=bool(metadata and metadata.get("notify")),
            turn_final=bool(metadata and metadata.get("is_turn_final")),
        )

    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not is_guest_chat_id(chat_id):
            return await super().send_or_update_status(
                chat_id,
                status_key,
                content,
                metadata=metadata,
            )
        state = self._guest_queries.get(str(chat_id))
        if state is None:
            return SendResult(success=False, error="Unknown guest query")
        return SendResult(success=True)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not is_guest_chat_id(chat_id):
            return await super().edit_message(
                chat_id,
                message_id,
                content,
                finalize=finalize,
                metadata=metadata,
            )

        state = self._guest_queries.get(str(chat_id))
        if state is None:
            return SendResult(success=False, error="Unknown guest query")
        return await self._publish_guest(
            str(chat_id),
            content,
            finalize=finalize,
            turn_final=bool(metadata and metadata.get("is_turn_final")),
        )

    async def send_typing(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if is_guest_chat_id(chat_id):
            return
        await super().send_typing(chat_id, metadata=metadata)


def _build_adapter(config):
    return GuestTelegramAdapter(config)


def _patch_stream_consumer_turn_final_metadata() -> None:
    """Bridge explicit turn finality on Hermes cores that drop it before edits.

    Current StreamConsumer builds know whether a finalized frame ends the whole
    turn, but their private ``_edit_message`` helper forwards only static
    metadata. Guest rich content must never infer finality from ``finalize``
    alone because segment, split, and cancellation boundaries also finalize.
    """
    try:
        from gateway.stream_consumer import GatewayStreamConsumer
    except Exception:
        return

    current_send_or_edit = getattr(GatewayStreamConsumer, "_send_or_edit", None)
    current_edit_message = getattr(GatewayStreamConsumer, "_edit_message", None)
    if not callable(current_send_or_edit) or not callable(current_edit_message):
        return
    if getattr(current_send_or_edit, "_telegram_guest_turn_final_patch", False):
        return

    try:
        send_params = inspect.signature(current_send_or_edit).parameters
        edit_params = inspect.signature(current_edit_message).parameters
    except (TypeError, ValueError):
        return
    # A newer core owns this contract itself; never stack a compatibility shim.
    if "is_turn_final" in edit_params:
        return
    # Older cores that do not expose the lifecycle fact cannot be patched safely.
    if "is_turn_final" not in send_params:
        return

    turn_final_attr = "_telegram_guest_current_turn_final"
    missing = object()

    async def _send_or_edit(
        self,
        text: str,
        *,
        finalize: bool = False,
        is_turn_final: bool = True,
    ) -> bool:
        if not is_guest_chat_id(getattr(self, "chat_id", "")):
            return await current_send_or_edit(
                self,
                text,
                finalize=finalize,
                is_turn_final=is_turn_final,
            )
        previous = getattr(self, turn_final_attr, missing)
        setattr(self, turn_final_attr, bool(is_turn_final))
        try:
            return await current_send_or_edit(
                self,
                text,
                finalize=finalize,
                is_turn_final=is_turn_final,
            )
        finally:
            if previous is missing:
                try:
                    delattr(self, turn_final_attr)
                except AttributeError:
                    pass
            else:
                setattr(self, turn_final_attr, previous)

    async def _edit_message(
        self,
        *,
        message_id: str,
        content: str,
        finalize: bool = False,
    ):
        if not is_guest_chat_id(getattr(self, "chat_id", "")):
            return await current_edit_message(
                self,
                message_id=message_id,
                content=content,
                finalize=finalize,
            )

        kwargs = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "content": content,
            "finalize": finalize,
        }
        metadata = dict(self.metadata) if self.metadata else {}
        if finalize:
            metadata["is_turn_final"] = bool(
                getattr(self, turn_final_attr, False)
            )
        if metadata:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                ):
                    kwargs["metadata"] = metadata
            except (TypeError, ValueError):
                pass
        return await self.adapter.edit_message(**kwargs)

    _send_or_edit._telegram_guest_turn_final_patch = True  # type: ignore[attr-defined]
    _edit_message._telegram_guest_turn_final_patch = True  # type: ignore[attr-defined]
    GatewayStreamConsumer._send_or_edit = _send_or_edit
    GatewayStreamConsumer._edit_message = _edit_message


_patch_stream_consumer_turn_final_metadata()


def _wire_guest_handler(app, adapter) -> None:
    """Register Guest updates through Hermes' native platform-handler API."""
    if not isinstance(adapter, GuestTelegramAdapter):
        logger.warning(
            "[Telegram Guest] Expected GuestTelegramAdapter, got %s",
            type(adapter).__name__,
        )
        return
    adapter._ensure_guest_handler(app)


def register(ctx) -> None:
    ctx.register_platform_handler("telegram", _wire_guest_handler)
    ctx.register_platform(
        name="telegram",
        label="Telegram",
        adapter_factory=_build_adapter,
        check_fn=telegram_base.check_telegram_requirements,
        is_connected=telegram_base._is_connected,
        required_env=["TELEGRAM_BOT_TOKEN"],
        install_hint="Run `hermes setup` to install Telegram support.",
        setup_fn=telegram_base.interactive_setup,
        apply_yaml_config_fn=telegram_base._apply_yaml_config,
        allowed_users_env="TELEGRAM_ALLOWED_USERS",
        allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
        cron_deliver_env_var="TELEGRAM_HOME_CHANNEL",
        standalone_sender_fn=telegram_base._standalone_send,
        max_message_length=4096,
        emoji="✈️",
        allow_update_command=True,
    )
