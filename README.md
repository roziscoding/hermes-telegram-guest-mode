# Hermes Telegram Guest Mode

Native [Telegram Guest Bot](https://core.telegram.org/bots/features#guest-bots) support for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Guest Mode lets someone mention your bot in a Telegram chat where the bot is **not a member**. Telegram sends the bot limited context and allows one response. This plugin adds that update and response path while delegating ordinary Telegram traffic to Hermes' bundled adapter.

> **Compatibility:** verified with Hermes `v0.20.0` and `python-telegram-bot 22.6`. The plugin subclasses private Telegram adapter methods, so a future Hermes update may require a compatibility fix.

## Install with Hermes

Send your Hermes agent this repository URL with the following request:

> Install the plugin from https://github.com/roziscoding/hermes-telegram-guest-mode. Inspect the repository first, then follow its README. Do not overwrite an unrelated existing plugin directory. After enabling it, restart the gateway, verify Telegram reconnects, and roll back by disabling the plugin if startup fails.

Hermes should inspect the repository, install it through the native plugin manager, restart the gateway, and verify connectivity.

## Manual installation

```bash
hermes plugins install https://github.com/roziscoding/hermes-telegram-guest-mode --enable
hermes gateway restart
hermes gateway status
```

Hermes reads `name: telegram-guest` from the manifest and installs the repository under that canonical plugin name. The plugin does not need permission to override built-in tools.

If `telegram-guest` is already installed, inspect the existing plugin and its Git origin instead of using `--force` blindly.

Then enable Guest Mode in Telegram:

1. Open [@BotFather](https://t.me/BotFather).
2. Select your bot.
3. Open **Bot Settings → Guest Mode** and enable it.
4. Mention the bot from a group or private conversation where it is not a member.

## Updating

```bash
hermes plugins update telegram-guest
hermes gateway restart
```

## How it works

The plugin registers a thin `GuestTelegramAdapter` subclass as the `telegram` platform:

- requests the `guest_message` update type;
- installs a handler before Telegram polling starts;
- accepts text or captions plus Hermes-supported direct and replied-to content through a registry-based dispatcher: photos, videos, audio, voice notes, documents, locations/venues, and stickers;
- preserves quoted voice notes as `VOICE` events so Hermes sends them through automatic speech-to-text;
- preserves Hermes' normal inbound message pipeline;
- authorizes channel-profile invocations through `sender_chat` instead of Telegram's fake `Channel_Bot` user, using an explicit `group_allow_from` override when present and the global `allow_from` list otherwise;
- immediately publishes `✨ Thinking` after authorization using Telegram custom emoji `5463297803235113601`, then replaces it in place as soon as the first streamed token arrives;
- sends the first visible frame with `answerGuestQuery`, preserves the returned
  `inline_message_id`, and updates that same response with `editMessageText`;
- keeps streaming previews on the tolerant plain-text path, then upgrades eligible final responses (tables, task lists, details, and block math) to Bot API rich messages in place;
- falls back from a permanently rejected rich final to MarkdownV2, and from rejected MarkdownV2 to plain text, without duplicating ambiguous edits;
- suppresses internal lifecycle/status bubbles and prevents auxiliary footer sends from replacing a completed Guest response;
- retains the complete pending replacement after an edit failure so Hermes can retry without replacing the response with a continuation-only tail;
- delegates normal sends, edits, typing indicators, and ordinary updates to the bundled adapter;
- authorizes the Guest caller through Hermes' user-level Telegram policy, including adapter allowlists, environment allowlists, pairing, and global policy.

If the guest handler cannot be installed, ordinary Telegram startup continues. A normal adapter incompatibility can still prevent this subclass from loading; verify the gateway after every Hermes update.

## Current limitations

- Guest responses are text/rich-text only. Inbound polls, contacts, dice, animations, video notes, and other Telegram message classes without a Hermes `MessageType` are currently ignored unless their text/caption is independently useful.
- Telegram permits one initial guest response. The plugin uses inline-message edits to update that response during streaming and after tool calls; it cannot send a second independent guest message. After any failed or ambiguous `answerGuestQuery` attempt, it fails closed instead of risking a second initial response. Attempted query IDs remain in a separate 24-hour process-local replay guard even if detailed query state is evicted.
- Interactive polls/clarification prompts and multiple assistant messages cannot be delivered through a guest query.
- Initial and legacy Guest text is capped at Telegram's 4,096 UTF-16-unit limit. Eligible streamed responses can upgrade in place to a final rich message up to Telegram's 32,768-character rich limit.
- Rich final promotion requires a Hermes core that supplies explicit `is_turn_final` stream metadata; cores that do not propagate it safely remain on the legacy MarkdownV2/plain path.
- The plugin depends on private Hermes adapter methods and may need updates when Hermes changes them.

## Troubleshooting

Check the gateway first:

```bash
hermes gateway status
```

On Linux installations managed by systemd, inspect recent logs with:

```bash
journalctl --user -u hermes-gateway --since "10 minutes ago" --no-pager
```

On macOS or foreground installations, inspect the terminal/log destination where `hermes gateway` is running.

If normal Telegram connectivity fails, disable the plugin and restart:

```bash
hermes plugins disable telegram-guest
hermes gateway restart
```

If guest mentions are ignored but normal Telegram works:

- confirm Guest Mode is enabled in BotFather;
- confirm the person mentioning the bot is authorized in the Hermes Telegram configuration;
- confirm the logs contain `polling accepts guest_message` after startup.

## Uninstall

Disable the plugin and restore the bundled adapter first:

```bash
hermes plugins disable telegram-guest
hermes gateway restart
hermes plugins remove telegram-guest
```

The bundled Telegram adapter is restored by the restart before the plugin files are removed.

## Development

Run the tests with a Python environment that can import the Hermes source tree and its Telegram dependencies:

```bash
python -m pytest -q
```

## License

[MIT](LICENSE)
