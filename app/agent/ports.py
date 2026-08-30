"""Aegis Agent and optional external-AI provider protocols."""

from collections.abc import Mapping
from typing import Protocol

from app.agent.context import AegisAgentContext
from app.agent.models import AegisAgentResult


class AegisAgent(Protocol):
    def analyze(self, context: AegisAgentContext) -> AegisAgentResult: ...


class AIAnalysisProvider(Protocol):
    """Untrusted provider returns data only; it receives no application capabilities."""

    def analyze(self, normalized_context: Mapping[str, object]) -> Mapping[str, object]: ...
