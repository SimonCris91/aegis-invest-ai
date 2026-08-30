"""Normalized read-client errors."""


class EtoroDataError(RuntimeError):
    """Raised when requested read-side data is unavailable or inconsistent."""
