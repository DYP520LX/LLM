"""Core exception types."""


class DAgentError(Exception):
    """Base class for all DAgent errors."""


class ConfigError(DAgentError):
    """Raised when the configuration file is missing or invalid."""


class LLMError(DAgentError):
    """Raised when the LLM request fails or returns an invalid response."""
