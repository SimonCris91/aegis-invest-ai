"""Bounded quote observation and causal readback; never alters candle inputs."""
from datetime import timedelta
from decimal import Decimal

from app.brokers.etoro.client import EtoroApiError
from app.brokers.etoro.mapping import EtoroMappingError
from app.domain.enums import MarketStatus
from app.intelligence.profiles import asset_strategy_profiles_for_confidence_profile


def observe_runtime_quotes(*, client, store, instruments, cutoff, clock,
                           batch_size=8, max_age=timedelta(seconds=120)):
    if not 1 <= batch_size <= 8:
        raise ValueError("quote batch size must be between 1 and 8")
    # Choose the least recently received instruments so larger universes rotate.
    targets = tuple(i for i in instruments if i.broker == "etoro"
                    and i.numeric_instrument_id is not None
                    and i.market_status in {MarketStatus.OPEN, MarketStatus.CONTINUOUS_24_7})
    ordered = sorted(targets, key=lambda i: (store.last_received(
        provider="etoro", instrument_id=i.numeric_instrument_id, symbol=i.symbol), i.key))
    report = {"cutoff": cutoff.isoformat(), "attempted": 0, "persisted": 0,
              "errors": [], "eligible": len(targets), "available_at_cutoff": [],
              "role": "CURRENT_SPREAD_ASSESSMENT", "broker_writes": 0,
              "spread_assessments": []}
    cooling = store.cooling_down(now=clock())
    report["cooling_down"] = cooling
    for item in (() if cooling else ordered[:batch_size]):
        report["attempted"] += 1
        try:
            quote = client.quote(item.numeric_instrument_id, item.symbol, currency=item.currency)
            received = clock()
            if (quote.instrument_id, quote.symbol, quote.currency) != (
                    item.numeric_instrument_id, item.symbol, item.currency):
                raise ValueError("quote identity mismatch")
            store.record(provider="etoro", quote=quote, received_at=received)
            report["persisted"] += 1
        except EtoroApiError as exc:
            report["errors"].append({"symbol": item.symbol, "status": exc.status})
            if exc.status == 429:
                store.set_cooldown(until=clock() + timedelta(minutes=15))
                break
        except (EtoroMappingError, ValueError, TypeError) as exc:
            report["errors"].append({"symbol": item.symbol, "error": type(exc).__name__})
    # Newly received quotes normally cannot satisfy the already-fixed cutoff.
    # They become usable by the next poll, subject to freshness.
    for item in targets:
        quote = store.at_cutoff(provider="etoro", instrument_id=item.numeric_instrument_id,
            symbol=item.symbol, currency=item.currency, cutoff=cutoff, max_age=max_age)
        if quote is not None:
            report["available_at_cutoff"].append(quote.model_dump(mode="json"))
        report["spread_assessments"].append(assess_current_spread(
            instrument=item, quote=quote, cutoff=cutoff, max_age=max_age))
    return report


def assess_current_spread(*, instrument, quote, cutoff, max_age):
    """Assess current microstructure, not historical score or order permission.

    Caller supplies only quotes admitted by the bitemporal store selector.
    """
    profile = next((p for p in asset_strategy_profiles_for_confidence_profile("V2_B_GUARDED")
                    if p.asset_class == instrument.asset_class), None)
    limit = profile.maximum_spread_for_positive_liquidity if profile else None
    result = {"symbol": instrument.symbol, "instrument_id": instrument.broker_instrument_id,
        "currency": instrument.currency.value, "cutoff": cutoff.isoformat(),
        "status": "QUOTE_UNAVAILABLE_AT_CUTOFF", "bid": None, "ask": None,
        "provider_at": None, "age_seconds": None, "spread_ratio": None, "spread_bps": None,
        "profile_spread_limit": str(limit) if limit is not None else None,
        "historical_score_changed": False, "authorizes_order": False}
    if quote is None:
        return result
    if (quote.instrument_id, quote.symbol, quote.currency) != (
            instrument.numeric_instrument_id, instrument.symbol, instrument.currency):
        result["status"] = "QUOTE_IDENTITY_MISMATCH"
        return result
    age = cutoff - quote.as_of
    if age < timedelta(0) or age > max_age or quote.bid is None or quote.ask is None:
        result["status"] = "QUOTE_INVALID_AT_CUTOFF"
        return result
    mid = (quote.bid + quote.ask) / Decimal("2")
    spread = (quote.ask - quote.bid) / mid
    result.update(bid=str(quote.bid), ask=str(quote.ask), provider_at=quote.as_of.isoformat(),
        age_seconds=age.total_seconds(), spread_ratio=str(spread),
        spread_bps=str(spread * Decimal("10000")),
        status="PROFILE_UNAVAILABLE" if limit is None else
            "WITHIN_PROFILE_LIMIT" if spread <= limit else "ABOVE_PROFILE_LIMIT")
    return result
