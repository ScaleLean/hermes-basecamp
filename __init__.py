if __package__:
    from .adapter import register
else:
    register = None

__all__ = ["register"]
