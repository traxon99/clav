class ClavError(Exception):
    """Base class for all CLAV-specific exceptions."""


class ConfigError(ClavError):
    """Raised when configuration is missing, malformed, or fails validation."""
