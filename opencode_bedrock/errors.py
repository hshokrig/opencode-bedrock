class BedrockError(Exception):
    """A user-facing command failure."""


class NotFoundError(BedrockError):
    """A requested OpenCode resource does not exist."""
