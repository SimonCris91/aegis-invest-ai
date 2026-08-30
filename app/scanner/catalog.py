"""In-memory normalized instrument catalog."""

from collections.abc import Iterable

from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.universe import UniversalInstrument
from app.policies.engine import AssetPolicyEngine


class InstrumentCatalog:
    def __init__(self, instruments: Iterable[UniversalInstrument] = ()) -> None:
        self._by_key = {instrument.key: instrument for instrument in instruments}

    def upsert_many(self, instruments: Iterable[UniversalInstrument]) -> None:
        for instrument in instruments:
            self._by_key[instrument.key] = instrument

    def all(self) -> tuple[UniversalInstrument, ...]:
        return tuple(self._by_key.values())

    def query(
        self,
        *,
        broker: str | None = None,
        asset_class: AssetClass | None = None,
        market_status: MarketStatus | None = None,
        currency: Currency | None = None,
        eligible: bool | None = None,
    ) -> tuple[UniversalInstrument, ...]:
        result = self.all()
        if broker is not None:
            result = tuple(item for item in result if item.broker.casefold() == broker.casefold())
        if asset_class is not None:
            result = tuple(item for item in result if item.asset_class is asset_class)
        if market_status is not None:
            result = tuple(item for item in result if item.market_status is market_status)
        if currency is not None:
            result = tuple(item for item in result if item.currency is currency)
        if eligible is not None:
            result = tuple(
                item
                for item in result
                if item.broker_eligibility is not None
                and item.broker_eligibility.verified is eligible
            )
        return result

    def query_policy_compatible(
        self, policy_engine: AssetPolicyEngine
    ) -> tuple[UniversalInstrument, ...]:
        compatible: list[UniversalInstrument] = []
        for instrument in self.all():
            if policy_engine.policy_for(instrument.asset_class).enabled:
                compatible.append(instrument)
        return tuple(compatible)
