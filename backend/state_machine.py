"""Small explicit state-machine guard shared by workflow services."""


class InvalidState(RuntimeError):
    """The requested transition is not an allowed edge from the current state."""


def transition(current: str, target: str, allowed: dict[str, set[str]]) -> None:
    """Validate one declared workflow edge without mutating caller state."""
    if not isinstance(current, str) or not isinstance(target, str):
        raise InvalidState("workflow states must be strings")
    if current not in allowed or target not in allowed[current]:
        raise InvalidState(f"transition from {current!r} to {target!r} is not allowed")
