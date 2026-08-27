# Agent installation guide

This repository is a standalone Hermes Agent plugin. When a user explicitly asks you to install it:

1. Inspect `README.md`, `plugin.yaml`, and `adapter.py` before making changes.
2. Resolve the active Hermes profile and inspect whether `telegram-guest` is already installed.
3. Never overwrite an existing plugin blindly. If it exists, inspect its Git origin and working tree before deciding whether to update or stop.
4. Confirm Hermes is v0.20.6 or newer (the plugin requires `ctx.register_platform_handler`).
5. Run `hermes plugins install https://github.com/roziscoding/hermes-telegram-guest-mode --enable`, then `hermes gateway restart`.
6. Verify `hermes gateway status` and normal Telegram connectivity. Guest Mode still has to be enabled separately in BotFather.
7. If startup or Telegram connectivity fails, run `hermes plugins disable telegram-guest` and restart the gateway to restore the bundled adapter.

Do not edit the Hermes core checkout to install this plugin. Do not request or expose the Telegram bot token.
