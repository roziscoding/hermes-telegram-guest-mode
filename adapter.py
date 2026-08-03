from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from gateway.platforms.base import SendResult
from plugins.platforms.telegram import adapter as telegram_base
from telegram import InlineQueryResultArticle, InputTextMessageContent, Message, Update
from telegram.ext import TypeHandler

GUEST_UPDATE_TYPE = "guest_message"
GUEST_CHAT_PREFIX = "guest:"

logger = logging.getLogger(__name__)
_UPDATE_TYPES_LOCK = asyncio.Lock()


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
    answer_attempted: bool = False
    answer_error: str | None = None
    answered: bool = False
    inline_message_id: str | None = None
    answer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class GuestTelegramAdapter(telegram_base.TelegramAdapter):
    """Thin native Guest Mode layer over Hermes' bundled Telegram adapter."""

    MAX_RETAINED_QUERIES = 1024
    GUEST_STREAM_CONSUMER_LIMIT = 2_147_483_647

    def __init__(self, config):
        super().__init__(config)
        self._guest_queries: dict[str, GuestQueryState] = {}
        self._guest_handler_apps: set[int] = set()

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name == "_app" and value is not None and hasattr(self, "_guest_handler_apps"):
            self._ensure_guest_handler(value)

    @staticmethod
    def with_guest_update_type(update_types: Iterable[str]) -> tuple[str, ...]:
        result = tuple(update_types)
        if GUEST_UPDATE_TYPE not in result:
            result += (GUEST_UPDATE_TYPE,)
        return result

    def _remember_guest_query(self, chat_id: str, query_id: str) -> GuestQueryState:
        state = self._guest_queries.get(chat_id)
        if state is None:
            if len(self._guest_queries) >= self.MAX_RETAINED_QUERIES:
                self._guest_queries.pop(next(iter(self._guest_queries)))
            state = GuestQueryState(query_id=query_id)
            self._guest_queries[chat_id] = state
        return state

    def _ensure_guest_handler(self, app) -> bool:
        """Install the catch-all guest handler without risking normal Telegram."""
        installed = getattr(self, "_guest_handler_apps", None)
        if installed is None:
            installed = self._guest_handler_apps = set()
        app_id = id(app)
        if app_id in installed:
            return True
        try:
            app.add_handler(TypeHandler(Update, self._handle_guest_update), group=-100)
        except Exception:
            logger.warning(
                "[Telegram Guest] Could not install guest handler; normal Telegram remains active",
                exc_info=True,
            )
            return False
        installed.add(app_id)
        return True

    @staticmethod
    def _configured_guest_user_ids(config) -> set[str]:
        values: list[Any] = []
        extra = getattr(config, "extra", {}) or {}
        configured = extra.get("allow_from")
        if isinstance(configured, (list, tuple, set)):
            values.extend(configured)
        elif configured:
            values.extend(str(configured).split(","))

        return {str(value).strip() for value in values if str(value).strip()}

    def _guest_caller_authorized(self, message: Message) -> bool:
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", "") or "").strip()
        if not user_id:
            return False

        # An explicit adapter allowlist is authoritative, matching Hermes' DM
        # authorization semantics. Guest queries are authorized as the caller,
        # not as the external chat where the bot is only temporarily mentioned.
        extra = getattr(self.config, "extra", {}) or {}
        if extra.get("allow_from") is not None:
            allowed = self._configured_guest_user_ids(self.config)
            return user_id in allowed or "*" in allowed

        # Use the profile-scoped authorization callback injected by GatewayRunner.
        # Guest queries are authorized as a DM from the caller, not as the external
        # chat where the bot is only temporarily mentioned.
        auth_check = getattr(self, "_authorization_check", None)
        if callable(auth_check):
            try:
                return bool(auth_check(user_id, "dm", user_id))
            except Exception:
                logger.warning(
                    "[Telegram Guest] Central caller authorization failed; denying request",
                    exc_info=True,
                )
                return False

        return False

    async def _handle_guest_update(self, update: Update, context) -> None:
        raw = (getattr(update, "api_kwargs", None) or {}).get(GUEST_UPDATE_TYPE)
        if not isinstance(raw, dict):
            return
        query_id = str(raw.get("guest_query_id") or "").strip()
        if not query_id:
            return
        try:
            message = Message.de_json(raw, self._bot)
        except Exception:
            logger.warning("[Telegram Guest] Invalid guest_message payload", exc_info=True)
            return
        if not message or not getattr(message, "text", None):
            return
        if not self._guest_caller_authorized(message):
            logger.warning(
                "[Telegram Guest] Blocked unauthorized caller %s",
                getattr(getattr(message, "from_user", None), "id", None),
            )
            return

        synthetic_chat_id = guest_chat_id(query_id)
        event = self._build_message_event(
            message,
            telegram_base.MessageType.TEXT,
            update_id=getattr(update, "update_id", None),
        )
        event.source.chat_id = synthetic_chat_id
        event.source.chat_type = "guest"
        self._remember_guest_query(synthetic_chat_id, query_id)
        await self.handle_message(event)

    async def _start_polling_once(self, app, **kwargs):
        self._ensure_guest_handler(app)
        async with _UPDATE_TYPES_LOCK:
            original = Update.ALL_TYPES
            Update.ALL_TYPES = self.with_guest_update_type(original)
            try:
                result = await super()._start_polling_once(app, **kwargs)
                logger.info(
                    "[Telegram Guest] Guest handler installed; polling accepts guest_message"
                )
                return result
            finally:
                Update.ALL_TYPES = original

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Enable guest updates around the parent connect; fail open to normal Telegram."""
        if os.getenv("TELEGRAM_WEBHOOK_URL", "").strip():
            # Polling is patched in _start_polling_once. Webhook setup consumes
            # Update.ALL_TYPES directly in connect(), so serialize only that path.
            async with _UPDATE_TYPES_LOCK:
                original = Update.ALL_TYPES
                Update.ALL_TYPES = self.with_guest_update_type(original)
                try:
                    connected = await super().connect(is_reconnect=is_reconnect)
                finally:
                    Update.ALL_TYPES = original
        else:
            connected = await super().connect(is_reconnect=is_reconnect)
        if connected and getattr(self, "_app", None) is not None:
            # Polling installs before getUpdates in _start_polling_once. This
            # second idempotent call covers webhook mode and future parent paths.
            self._ensure_guest_handler(self._app)
        return connected

    async def _publish_guest(
        self,
        chat_id: str,
        content: str,
        *,
        allow_after_answer: bool = True,
    ) -> SendResult:
        state = self._guest_queries.get(chat_id)
        if state is None:
            return SendResult(success=False, error="Unknown guest query")
        async with state.answer_lock:
            if state.answered and not allow_after_answer:
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
            if telegram_base.utf16_len(text) > self.MAX_MESSAGE_LENGTH:
                text = self.truncate_message(
                    text, self.MAX_MESSAGE_LENGTH, len_fn=telegram_base.utf16_len
                )[0]
            state.latest_content = text

            if state.answered:
                if not state.inline_message_id:
                    raise GuestEditError(
                        "Guest response cannot be edited: missing inline_message_id"
                    )
                if text == state.delivered_content:
                    return SendResult(
                        success=True,
                        message_id=state.inline_message_id,
                    )
                try:
                    raw_response = await self._bot._post(
                        "editMessageText",
                        {
                            "inline_message_id": state.inline_message_id,
                            "text": text,
                        },
                    )
                except Exception as exc:
                    raise GuestEditError(
                        telegram_base._redact_telegram_error_text(exc)
                    ) from exc
                state.delivered_content = text
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
            result_id = hashlib.sha256(state.query_id.encode("utf-8")).hexdigest()[:32]
            result = InlineQueryResultArticle(
                id=result_id,
                title=getattr(self._bot, "first_name", None) or "Hermes",
                input_message_content=InputTextMessageContent(message_text=text),
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
        return await self._publish_guest(str(chat_id), content)

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


def register(ctx) -> None:
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
