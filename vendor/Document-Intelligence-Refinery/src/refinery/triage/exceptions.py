"""Custom exceptions for the Triage Agent."""


class RefineryTriageError(Exception):
    """Raised when PDF cannot be opened or triage fails (e.g. corrupted file)."""

    pass
