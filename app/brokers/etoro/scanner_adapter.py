"""Read-only eToro adapter for the broker-neutral market scanner."""

from datetime import datetime

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.models import BrokerCapabilities, DemoEligibility
from app.domain.enums import Currency, OperatingMode
from app.domain.market import MarketQuote
from app.domain.universe import BrokerEligibilitySnapshot, UniversalInstrument


class EtoroMarketScannerAdapter:
    def __init__(
        self,
        client: EtoroReadClient,
        *,
        search_text: str | None = None,
        max_pages: int = 1,
        currency: Currency = Currency.USD,
    ) -> None:
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        self._client = client
        self._search_text = search_text
        self._max_pages = max_pages
        self._currency = currency

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="etoro-official-api-read-only",
            mode=OperatingMode.SHADOW,
            authenticated_reads=True,
            demo_execution=False,
            real_execution=False,
        )

    def discover_instruments(
        self, *, as_of: datetime, limit: int
    ) -> tuple[UniversalInstrument, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        discovered: list[UniversalInstrument] = []
        page_size = min(limit, 100)
        for page_number in range(1, self._max_pages + 1):
            page = self._client.discover_instruments(
                as_of=as_of,
                page_size=page_size,
                page_number=page_number,
                search_text=self._search_text,
            )
            discovered.extend(page)
            if len(discovered) >= limit or len(page) < page_size:
                break
        return tuple(discovered[:limit])

    def quote(self, instrument: UniversalInstrument, *, as_of: datetime) -> MarketQuote:
        instrument_id = instrument.numeric_instrument_id
        if instrument_id is None:
            raise ValueError("eToro instrument id must be numeric")
        return self._client.quote(instrument_id, instrument.symbol, currency=self._currency)

    def eligibility(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> BrokerEligibilitySnapshot:
        instrument_id = instrument.numeric_instrument_id
        if instrument_id is None:
            raise ValueError("eToro instrument id must be numeric")
        try:
            eligibility = self._client.demo_eligibility(
                instrument_id,
                instrument.symbol,
                currency=self._currency,
            )
        except EtoroApiError as exc:
            return BrokerEligibilitySnapshot(
                broker=instrument.broker,
                broker_instrument_id=instrument.broker_instrument_id,
                symbol=instrument.symbol,
                checked_at=as_of,
                currency=self._currency,
                verified=False,
                allow_open=False,
                reason=exc.category.value,
            )
        return _to_universal_eligibility(instrument, eligibility, as_of=as_of)


def _to_universal_eligibility(
    instrument: UniversalInstrument, eligibility: DemoEligibility, *, as_of: datetime
) -> BrokerEligibilitySnapshot:
    return BrokerEligibilitySnapshot(
        broker=instrument.broker,
        broker_instrument_id=instrument.broker_instrument_id,
        symbol=instrument.symbol,
        checked_at=as_of,
        currency=eligibility.currency,
        verified=eligibility.verified,
        allow_open=eligibility.allow_open,
        allow_close=eligibility.allow_close,
        minimum_order_value=eligibility.minimum_position,
        max_units_per_order=eligibility.max_units_per_order,
        allowed_order_quantity_types=eligibility.allowed_order_quantity_types,
        settlement_type=eligibility.settlement_type,
        leverage_configs=(eligibility.leverage,),
    )
