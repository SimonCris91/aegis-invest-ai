"""Shared authoritative revalidation immediately before a Demo submission."""

from datetime import datetime

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.demo_preflight import _preflight_market_status
from app.brokers.etoro.mapping import EtoroMappingError
from app.domain.enums import MarketStatus


def fresh_demo_tradability(
    client: EtoroReadClient, instrument_id: int, symbol: str, as_of: datetime
) -> bool:
    try:
        resolution = client.resolve_instrument_id(instrument_id, symbol=symbol, as_of=as_of)
        return bool(
            resolution.instrument_id == instrument_id
            and resolution.resolved
            and resolution.verified
            and resolution.structurally_supported
            and _preflight_market_status(resolution) is MarketStatus.OPEN
            and resolution.is_currently_tradable is True
            and resolution.is_buy_enabled is True
        )
    except (EtoroApiError, EtoroMappingError, ValueError, TypeError, KeyError):
        return False
