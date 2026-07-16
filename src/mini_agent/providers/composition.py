"""Explicit Provider compositions for production and offline callers."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from mini_agent.application.ports import IDGenerator, ModelProvider
from mini_agent.configuration import ConfigurationError, EffectiveConfiguration
from mini_agent.providers.openai_compatible import OpenAICompatibleModelProvider
from mini_agent.tools.contracts import ToolDefinition

type ProviderFactory = Callable[
    [EffectiveConfiguration, Sequence[ToolDefinition], IDGenerator], ModelProvider
]


class ProviderAuthenticationError(ConfigurationError):
    """Raised when production Provider authentication is not configured."""


def production_provider_factory(
    configuration: EffectiveConfiguration,
    tool_definitions: Sequence[ToolDefinition],
    id_generator: IDGenerator,
) -> ModelProvider:
    """Build the real Provider, refusing to turn missing auth into a Fake run."""

    if configuration.api_key is None or not configuration.api_key.strip():
        raise ProviderAuthenticationError(
            "Provider authentication is unavailable. Set a non-blank API key in "
            "the MINI_AGENT_API_KEY environment variable and retry; API keys are "
            "read only from the environment, never from TOML or CLI options."
        )
    return OpenAICompatibleModelProvider.from_configuration(
        configuration,
        tool_definitions=tool_definitions,
        id_generator=id_generator,
    )


__all__ = [
    "ProviderAuthenticationError",
    "ProviderFactory",
    "production_provider_factory",
]
