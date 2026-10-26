"""Typed failures shared across adapters without coupling them to an agent implementation."""


class AgentBlockedError(RuntimeError):
    """A systematic Agent failure that must stop new work until fixed."""

    health_state = "blocked"


class AgentAuthenticationError(AgentBlockedError):
    """The Codex model channel rejected credentials or model access."""

    health_state = "authentication_required"


class AgentConfigurationError(AgentBlockedError):
    """The selected model or command cannot satisfy the stage contract."""

    health_state = "configuration_required"
