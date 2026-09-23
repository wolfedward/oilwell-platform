"""Stable, safe-to-display registry errors, independent of storage drivers."""


class RegistryError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
