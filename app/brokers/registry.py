"""Broker capability registry for current and future adapters."""

from app.brokers.models import BrokerCapabilities
from app.domain.base import FrozenDomainModel
from app.domain.enums import OperatingMode


class BrokerRegistryEntry(FrozenDomainModel):
    broker: str
    capabilities: BrokerCapabilities
    live_adapter_available: bool
    notes: tuple[str, ...] = ()


class BrokerRegistry:
    def __init__(self, entries: tuple[BrokerRegistryEntry, ...]) -> None:
        self._entries = {entry.broker.casefold(): entry for entry in entries}

    def get(self, broker: str) -> BrokerRegistryEntry | None:
        return self._entries.get(broker.casefold())

    def list(self) -> tuple[BrokerRegistryEntry, ...]:
        return tuple(self._entries.values())


def default_broker_registry() -> BrokerRegistry:
    return BrokerRegistry(
        (
            BrokerRegistryEntry(
                broker="etoro",
                capabilities=BrokerCapabilities(
                    provider="etoro-official-api-read-only",
                    mode=OperatingMode.SHADOW,
                    authenticated_reads=True,
                    demo_execution=False,
                    real_execution=False,
                ),
                live_adapter_available=True,
                notes=("official eToro API read-only adapter",),
            ),
            BrokerRegistryEntry(
                broker="fake",
                capabilities=BrokerCapabilities(
                    provider="fake-broker",
                    mode=OperatingMode.OFFLINE_PAPER,
                    authenticated_reads=False,
                    demo_execution=False,
                    real_execution=False,
                ),
                live_adapter_available=True,
                notes=("deterministic offline adapter for portability tests",),
            ),
            BrokerRegistryEntry(
                broker="interactive-brokers",
                capabilities=BrokerCapabilities(
                    provider="interactive-brokers-readiness-only",
                    mode=OperatingMode.SHADOW,
                    authenticated_reads=False,
                    demo_execution=False,
                    real_execution=False,
                ),
                live_adapter_available=False,
                notes=("future adapter placeholder; no undocumented integration implemented",),
            ),
            BrokerRegistryEntry(
                broker="alpaca",
                capabilities=BrokerCapabilities(
                    provider="alpaca-readiness-only",
                    mode=OperatingMode.SHADOW,
                    authenticated_reads=False,
                    demo_execution=False,
                    real_execution=False,
                ),
                live_adapter_available=False,
                notes=("future adapter placeholder; no undocumented integration implemented",),
            ),
        )
    )
