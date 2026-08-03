# Hermes Telegram Guest Mode

Native [Telegram Guest Bot](https://core.telegram.org/bots/features#guest-bots) support for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Guest Mode lets someone mention your bot in a Telegram chat where the bot is **not a member**. Telegram sends the bot limited context and allows one response. This plugin adds that update and response path while delegating ordinary Telegram traffic to Hermes' bundled adapter.

> **Compatibility:** verified with Hermes `v0.19.1` and `python-telegram-bot 22.6`. The plugin subclasses private Telegram adapter methods, so a future Hermes update may require a compatibility fix.

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
- preserves Hermes' normal inbound message pipeline;
- sends the first visible frame with `answerGuestQuery`, preserves the returned
  `inline_message_id`, and updates that same response with `editMessageText`;
- applies Telegram MarkdownV2 formatting on the final inline edit, with a plain-text fallback when Telegram rejects the formatted payload;
- suppresses internal lifecycle/status bubbles and prevents auxiliary footer sends from replacing a completed Guest response;
- retains the complete pending replacement after an edit failure so Hermes can retry without replacing the response with a continuation-only tail;
- delegates normal sends, edits, typing indicators, and ordinary updates to the bundled adapter;
- authorizes the Guest caller through Hermes' user-level Telegram policy, including adapter allowlists, environment allowlists, pairing, and global policy.

If the guest handler cannot be installed, ordinary Telegram startup continues. A normal adapter incompatibility can still prevent this subclass from loading; verify the gateway after every Hermes update.

## Current limitations

- Text guest messages and text responses only.
- Telegram permits one initial guest response. The plugin uses inline-message edits to update that response during streaming and after tool calls; it cannot send a second independent guest message. After any failed or ambiguous `answerGuestQuery` attempt, it fails closed instead of risking a second initial response.
- Interactive polls/clarification prompts and multiple assistant messages cannot be delivered through a guest query.
- Guest replies are capped at Telegram's 4,096 UTF-16-unit text limit.
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
