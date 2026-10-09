"""Waveform boundary failures."""


class ContractError(ValueError):
    """Permanent malformed input, identified by a bounded protocol reason."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class WaveformError(RuntimeError):
    """System or business failure; callers must not quarantine it as bad input."""

    def __init__(self, code: str, message: str, status: int = 503):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
