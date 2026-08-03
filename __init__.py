if __package__:
    from .adapter import GuestTelegramAdapter, register
else:  # Direct repository loading by pytest/operator diagnostics.
    from adapter import GuestTelegramAdapter, register

__all__ = ["GuestTelegramAdapter", "register"]
