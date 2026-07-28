"""ClickClack messaging platform plugin for Hermes Agent."""

if __package__:
    from .adapter import register
else:  # pragma: no cover - standalone import used by some test runners
    from adapter import register

__all__ = ["register"]
