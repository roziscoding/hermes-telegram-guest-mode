# Telegram Guest Mode installed

1. Confirm `hermes --version` reports `v0.20.6` or newer.
2. Restart Hermes with `hermes gateway restart`.
3. Verify normal connectivity with `hermes gateway status`.
4. In [@BotFather](https://t.me/BotFather), open your bot's **Bot Settings → Guest Mode** and enable it.
5. Mention the bot in a Telegram conversation where it is not a member.

If Telegram does not reconnect, restore the bundled adapter:

```bash
hermes plugins disable telegram-guest
hermes gateway restart
```
