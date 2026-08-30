"""Provider-specific instrument mapping with fail-closed ambiguity handling."""

from collections.abc import Mapping
from decimal import Decimal

from app.data.models import (
    DataProviderStatus,
    InstrumentMappingResult,
    ProviderInstrumentReference,
)
from app.domain.enums import AssetClass
from app.domain.universe import UniversalInstrument


class InstrumentMappingService:
    def __init__(
        self,
        overrides: Mapping[tuple[str, str], ProviderInstrumentReference] | None = None,
    ) -> None:
        self._overrides = dict(overrides or {})

    def resolve(self, instrument: UniversalInstrument, *, provider: str) -> InstrumentMappingResult:
        override = self._overrides.get((provider, instrument.key))
        if override is not None:
            return InstrumentMappingResult(
                instrument=instrument,
                provider=provider,
                selected=override,
                candidates=(override,),
                status=DataProviderStatus.SUCCESS,
                reasons=("explicit verified provider mapping",),
            )
        if provider == "etoro":
            reference = ProviderInstrumentReference(
                provider=provider,
                provider_symbol=instrument.broker_instrument_id,
                broker=instrument.broker,
                broker_symbol=instrument.symbol,
                broker_instrument_id=instrument.broker_instrument_id,
                exchange=instrument.exchange,
                asset_class=instrument.asset_class,
                currency=instrument.currency,
                mapping_confidence=Decimal("1"),
                mapping_source="official broker instrument id",
                verified=True,
            )
            return InstrumentMappingResult(
                instrument=instrument,
                provider=provider,
                selected=reference,
                candidates=(reference,),
                status=DataProviderStatus.SUCCESS,
                reasons=("eToro historical data uses the broker instrumentId",),
            )
        if provider == "stooq" and instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            if instrument.exchange is None:
                return InstrumentMappingResult(
                    instrument=instrument,
                    provider=provider,
                    selected=None,
                    candidates=(),
                    status=DataProviderStatus.MAPPING_AMBIGUOUS,
                    reasons=("external equity mapping requires exchange metadata or an override",),
                )
            reference = ProviderInstrumentReference(
                provider=provider,
                provider_symbol=instrument.symbol.lower(),
                broker=instrument.broker,
                broker_symbol=instrument.symbol,
                broker_instrument_id=instrument.broker_instrument_id,
                exchange=instrument.exchange,
                asset_class=instrument.asset_class,
                currency=instrument.currency,
                mapping_confidence=Decimal("0.80"),
                mapping_source="provider symbol with broker exchange metadata",
                verified=False,
            )
            return InstrumentMappingResult(
                instrument=instrument,
                provider=provider,
                selected=reference,
                candidates=(reference,),
                status=DataProviderStatus.SUCCESS,
                reasons=("symbol mapped with exchange metadata; live validation still required",),
            )
        if instrument.asset_class is AssetClass.CRYPTO:
            return InstrumentMappingResult(
                instrument=instrument,
                provider=provider,
                selected=None,
                candidates=(),
                status=DataProviderStatus.MAPPING_AMBIGUOUS,
                reasons=("crypto mappings must be provider-verified; ticker inference is blocked",),
            )
        return InstrumentMappingResult(
            instrument=instrument,
            provider=provider,
            selected=None,
            candidates=(),
            status=DataProviderStatus.DATA_INSUFFICIENT,
            reasons=("no provider mapping is configured for this instrument",),
        )
