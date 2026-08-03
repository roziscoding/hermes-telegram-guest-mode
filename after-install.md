# Telegram Guest Mode installed

1. Restart Hermes with `hermes gateway restart`.
2. Verify normal connectivity with `hermes gateway status`.
3. In [@BotFather](https://t.me/BotFather), open your bot's **Bot Settings → Guest Mode** and enable it.
4. Mention the bot in a Telegram conversation where it is not a member.

If Telegram does not reconnect, restore the bundled adapter:

```bash
hermes plugins disable telegram-guest
hermes gateway restart
```
