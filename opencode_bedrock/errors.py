class BedrockError(Exception):
    """A user-facing command failure."""


class HTTPResponseError(BedrockError):
    """The OpenCode service returned a definite non-success HTTP response."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class NotFoundError(HTTPResponseError):
    """A requested OpenCode resource does not exist."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message, status)


class TransportError(BedrockError):
    """A request failed without a definite OpenCode HTTP response."""


class JSONWriteError(BedrockError):
    """A private JSON write failed, with an explicit replacement boundary."""

    def __init__(self, path: object, error: BaseException, *, committed: bool):
        self.committed = committed
        outcome = (
            "replacement committed but directory durability is uncertain"
            if committed
            else "replacement did not commit"
        )
        super().__init__(
            f"cannot prepare or write private JSON state {path}: {error}; {outcome}"
        )
