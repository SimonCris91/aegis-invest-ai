"""Strict normalization of verified eToro response fields."""

from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite

from pydantic import Field

from app.brokers.models import (
    AccountKind,
    BrokerAccountContext,
    BrokerIdentity,
    DemoEligibility,
    DemoPortfolioPosition,
    DemoPortfolioSnapshot,
    ExecutionState,
    InstrumentResolution,
)
from app.domain.base import FrozenDomainModel
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType, TradeSide
from app.domain.market import MarketQuote
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.universe import UniversalInstrument

SAFE_CLASSIFICATION_FIELDS = (
    "instrumentType",
    "instrumentTypeID",
    "instrumentTypeId",
    "internalAssetClassName",
    "internalAssetClassId",
    "internalCryptoTypeId",
    "assetClass",
    "assetType",
    "type",
    "instrumentClass",
    "securityType",
    "marketType",
    "category",
    "subCategory",
    "underlying",
    "exchangeID",
    "exchangeId",
    "internalExchangeName",
    "symbol",
)


class EtoroMappingError(ValueError):
    pass


class EtoroEligibilityDenied(EtoroMappingError):
    """The broker returned a valid instrument record that explicitly forbids opening."""


def map_identity(raw: object) -> BrokerIdentity:
    if not isinstance(raw, dict):
        raise EtoroMappingError("identity payload must be an object")
    try:
        return BrokerIdentity(
            stable_user_id=str(raw["gcid"]),
            demo_account_id=int(raw["demoCid"]),
            real_account_id=int(raw["realCid"]),
            username=str(raw["username"]),
            scopes=tuple(str(x) for x in raw.get("scopes", [])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EtoroMappingError("invalid identity payload") from exc


def map_demo_portfolio(raw: object, identity: BrokerIdentity) -> DemoPortfolioSnapshot:
    if not isinstance(raw, dict):
        raise EtoroMappingError("Demo portfolio payload must be an object")
    try:
        cid = int(raw["cid"])
        if cid != identity.demo_account_id:
            raise EtoroMappingError("Demo portfolio account does not match identity")
        totals = raw["accountTotals"]
        if not isinstance(totals, dict):
            raise TypeError
        instruments = raw.get("instrumentAggregates", [])
        if not isinstance(instruments, list):
            raise TypeError
        positions = tuple(_map_demo_position(item) for item in instruments)
        return DemoPortfolioSnapshot(
            context=BrokerAccountContext(
                stable_user_id=identity.stable_user_id,
                account_id=cid,
                kind=AccountKind.DEMO,
            ),
            as_of=_provider_datetime(raw["timestamp"]),
            currency=Currency(str(raw["accountCurrency"]).upper()),
            cash=Decimal(str(totals["accountAvailableCash"])),
            total_value=Decimal(str(totals["accountTotalValue"])),
            current_pnl=Decimal(str(totals["accountCurrentPnl"])),
            account_balance=Decimal(str(totals["accountBalance"])),
            positions=positions,
            position_ids=tuple(str(item["instrumentId"]) for item in instruments),
        )
    except EtoroMappingError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EtoroMappingError("invalid Demo portfolio payload") from exc


def _map_demo_position(raw: object) -> DemoPortfolioPosition:
    if not isinstance(raw, dict):
        raise TypeError
    units = Decimal(str(raw["netUnits"]))
    return DemoPortfolioPosition(
        instrument_id=int(raw["instrumentId"]),
        asset_currency=Currency(str(raw["assetCurrency"]).upper()),
        side=TradeSide.BUY if units >= 0 else TradeSide.SELL,
        units=abs(units),
        current_exposure=abs(Decimal(str(raw["netCurrentExposureAccountCurrency"]))),
        initial_exposure=abs(Decimal(str(raw["netInitialExposureAccountCurrency"]))),
        unrealized_pnl_account_currency=Decimal(str(raw["accountCurrencyReturn"])),
        unrealized_pnl_asset_currency=Decimal(str(raw["pnlAssetCurrency"])),
        leverage=Decimal(str(raw["avgLeverage"])),
        average_open_rate=Decimal(str(raw["avgOpenRate"])),
    )


def map_instrument_resolution(
    raw: object,
    *,
    symbol: str,
    as_of: datetime,
    expected_instrument_id: int | None = None,
) -> InstrumentResolution:
    if not isinstance(raw, dict):
        raise EtoroMappingError("instrument search payload must be an object")
    try:
        items = raw["items"]
        if not isinstance(items, list):
            raise TypeError
        item = next(
            value
            for value in items
            if (
                expected_instrument_id is not None
                and int(value["instrumentId"]) == expected_instrument_id
            )
            or (
                expected_instrument_id is None
                and str(value["internalSymbolFull"]).strip() == symbol.strip()
            )
        )
        instrument_id = int(item["instrumentId"])
        hidden = _optional_bool(item, "isHiddenFromClient")
        delisted = _optional_bool(item, "isDelisted")
        active = _optional_bool(item, "isActiveInPlatform")
        exchange_open = _optional_bool(item, "isExchangeOpen")
        is_open = _optional_bool(item, "isOpen")
        tradable = _optional_bool(item, "isCurrentlyTradable")
        buy_enabled = _optional_bool(item, "isBuyEnabled")
        is_internal = _optional_bool(item, "isInternalInstrument")
        structurally_supported, structural_status = _structural_support(
            delisted=delisted,
            hidden=hidden,
        )
        return InstrumentResolution(
            instrument_id=instrument_id,
            symbol=symbol,
            internal_symbol_full=str(item["internalSymbolFull"]),
            display_name=_optional_str(item, "displayname"),
            instrument_type=_optional_str(item, "instrumentType"),
            classification_metadata=safe_instrument_classification_metadata(item),
            classification_evidence_source="search",
            classification_status="RAW_METADATA_RETAINED",
            market_status=_market_status(item),
            is_exchange_open=exchange_open,
            is_open=is_open,
            is_currently_tradable=tradable,
            is_buy_enabled=buy_enabled,
            is_internal_instrument=is_internal,
            is_hidden_from_client=hidden,
            is_delisted=delisted,
            is_active_in_platform=active,
            current_rate=_optional_decimal(item, "currentRate"),
            resolved=True,
            structurally_supported=structurally_supported,
            structural_status=structural_status,
            verified=structurally_supported,
            as_of=as_of,
        )
    except StopIteration as exc:
        raise EtoroMappingError("instrument symbol was not resolved exactly") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise EtoroMappingError("invalid instrument search payload") from exc


def map_universal_instrument_search(
    raw: object, *, as_of: datetime, broker: str = "etoro"
) -> tuple[UniversalInstrument, ...]:
    if not isinstance(raw, dict):
        raise EtoroMappingError("instrument search payload must be an object")
    try:
        items = raw["items"]
        if not isinstance(items, list):
            raise TypeError
    except (KeyError, TypeError) as exc:
        raise EtoroMappingError("invalid instrument search payload") from exc

    instruments: list[UniversalInstrument] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = _universal_instrument_from_search_item(item, as_of=as_of, broker=broker)
        if normalized is not None:
            instruments.append(normalized)
    return tuple(instruments)


def _universal_instrument_from_search_item(
    item: dict[str, object], *, as_of: datetime, broker: str
) -> UniversalInstrument | None:
    try:
        instrument_id = int(str(item["instrumentId"]))
    except (KeyError, TypeError, ValueError):
        return None
    if instrument_id <= 0:
        return None
    symbol = _optional_str(item, "internalSymbolFull")
    if symbol is None:
        return None
    instrument_type = _optional_str(item, "instrumentType")
    asset_class = asset_class_from_etoro_instrument_type(instrument_type)
    classification = classify_etoro_instrument_metadata(
        safe_instrument_classification_metadata(item)
    )
    if classification.asset_class is not AssetClass.UNKNOWN:
        asset_class = classification.asset_class
    tags = tuple(
        tag
        for tag, active in (
            ("hidden-from-client", _optional_bool(item, "isHiddenFromClient") is True),
            ("delisted", _optional_bool(item, "isDelisted") is True),
            ("inactive-platform", _optional_bool(item, "isActiveInPlatform") is False),
            ("source:eToro-search", True),
        )
        if active
    )
    return UniversalInstrument(
        broker=broker,
        broker_instrument_id=str(instrument_id),
        symbol=symbol,
        display_name=_optional_str(item, "displayname"),
        asset_class=asset_class,
        market_status=_market_status(item),
        tradeable=_optional_bool(item, "isCurrentlyTradable"),
        buy_allowed=_optional_bool(item, "isBuyEnabled"),
        sell_allowed=None,
        short_allowed=None,
        leverage_available=None,
        max_leverage=Decimal("1"),
        settlement_type=_settlement_type_from_asset_class(asset_class),
        last_price=_optional_decimal(item, "currentRate"),
        price_timestamp=as_of if _optional_decimal(item, "currentRate") is not None else None,
        metadata_timestamp=as_of,
        tags=tags,
    )


def asset_class_from_etoro_instrument_type(instrument_type: str | None) -> AssetClass:
    if instrument_type is None:
        return AssetClass.UNKNOWN
    normalized = (
        instrument_type.replace("-", " ").replace("_", " ").replace("/", " ").strip().casefold()
    )
    compact = normalized.replace(" ", "")
    tokens = set(normalized.split())
    if "cfd" in tokens or "cfd" in compact:
        return AssetClass.CFD
    if "future" in tokens or "futures" in tokens or "future" in compact:
        return AssetClass.FUTURE
    if "option" in tokens or "options" in tokens or "option" in compact:
        return AssetClass.OPTION
    if "derivative" in tokens or "derivatives" in tokens or "derivative" in compact:
        return AssetClass.OTHER_DERIVATIVE
    if normalized in {"stock", "stocks", "equity", "equities"}:
        return AssetClass.EQUITY
    if normalized in {"etf", "etfs", "exchange traded fund", "exchange traded funds"}:
        return AssetClass.ETF
    if normalized in {
        "crypto",
        "cryptos",
        "cryptocurrency",
        "cryptocurrencies",
        "crypto currency",
        "crypto currencies",
        "crypto asset",
        "crypto assets",
    }:
        return AssetClass.CRYPTO
    if normalized in {"forex", "currency", "currencies", "fx"}:
        return AssetClass.FOREX
    if normalized in {"index", "indices"}:
        return AssetClass.INDEX
    if normalized in {"commodity", "commodities"}:
        return AssetClass.COMMODITY
    if normalized in {"bond", "bonds"}:
        return AssetClass.BOND
    if normalized in {"fund", "funds"}:
        return AssetClass.FUND
    if tokens & {"stock", "stocks", "equity", "equities", "share", "shares"}:
        return AssetClass.EQUITY
    if tokens & {"etf", "etfs"} or "exchangetradedfund" in compact:
        return AssetClass.ETF
    if tokens & {"crypto", "cryptos", "cryptocurrency", "cryptocurrencies"}:
        return AssetClass.CRYPTO
    if compact in {"cryptoasset", "cryptoassets", "cryptocurrency", "cryptocurrencies"}:
        return AssetClass.CRYPTO
    if tokens & {"forex", "currency", "currencies", "fx"}:
        return AssetClass.FOREX
    if tokens & {"index", "indices"}:
        return AssetClass.INDEX
    if tokens & {"commodity", "commodities"}:
        return AssetClass.COMMODITY
    if normalized in {"cash", "money market", "cash equivalent", "cash equivalents"}:
        return AssetClass.CASH_EQUIVALENT
    return AssetClass.UNKNOWN


class EtoroInstrumentClassification(FrozenDomainModel):
    asset_class: AssetClass
    evidence_source: str
    status: str
    evidence_fields: dict[str, str | int | bool] = Field(default_factory=dict)


def safe_instrument_classification_metadata(
    item: dict[str, object],
) -> dict[str, str | int | bool]:
    metadata: dict[str, str | int | bool] = {}
    for field in SAFE_CLASSIFICATION_FIELDS:
        value = item.get(field)
        if isinstance(value, (bool, int)):
            metadata[field] = value
        elif isinstance(value, str) and value.strip():
            metadata[field] = value.strip()
    return metadata


def classify_etoro_instrument_metadata(
    metadata: dict[str, str | int | bool],
    *,
    instrument_type_names: dict[int, str] | None = None,
) -> EtoroInstrumentClassification:
    if not metadata:
        return EtoroInstrumentClassification(
            asset_class=AssetClass.UNKNOWN,
            evidence_source="none",
            status="CLASSIFICATION_INSUFFICIENT",
        )

    text_fields = (
        "instrumentType",
        "internalAssetClassName",
        "assetClass",
        "assetType",
        "type",
        "instrumentClass",
        "securityType",
        "marketType",
        "category",
        "subCategory",
        "underlying",
    )
    for field in text_fields:
        value = metadata.get(field)
        if isinstance(value, str):
            asset_class = asset_class_from_etoro_instrument_type(value)
            if asset_class is not AssetClass.UNKNOWN:
                return EtoroInstrumentClassification(
                    asset_class=asset_class,
                    evidence_source=field,
                    status="CLASSIFIED_FROM_PROVIDER_TEXT",
                    evidence_fields={field: value},
                )

    for field in ("instrumentTypeID", "instrumentTypeId"):
        value = metadata.get(field)
        if isinstance(value, int) and instrument_type_names is not None:
            type_name = instrument_type_names.get(value)
            asset_class = asset_class_from_etoro_instrument_type(type_name)
            if asset_class is not AssetClass.UNKNOWN and type_name is not None:
                return EtoroInstrumentClassification(
                    asset_class=asset_class,
                    evidence_source=field,
                    status="CLASSIFIED_FROM_PROVIDER_TYPE_ID",
                    evidence_fields={field: value, "instrumentTypeName": type_name},
                )

    crypto_type = metadata.get("internalCryptoTypeId")
    if isinstance(crypto_type, int) and crypto_type > 0:
        return EtoroInstrumentClassification(
            asset_class=AssetClass.CRYPTO,
            evidence_source="internalCryptoTypeId",
            status="CLASSIFIED_FROM_PROVIDER_CRYPTO_TYPE_ID",
            evidence_fields={"internalCryptoTypeId": crypto_type},
        )

    return EtoroInstrumentClassification(
        asset_class=AssetClass.UNKNOWN,
        evidence_source="metadata",
        status="CLASSIFICATION_UNKNOWN",
        evidence_fields=metadata,
    )


def _settlement_type_from_asset_class(asset_class: AssetClass) -> SettlementType | None:
    if asset_class is AssetClass.CFD:
        return SettlementType.CFD
    if asset_class is AssetClass.FUTURE:
        return SettlementType.REAL_FUTURES
    if asset_class in {AssetClass.OPTION, AssetClass.OTHER_DERIVATIVE}:
        return SettlementType.MARGIN_TRADE
    return None


def _optional_bool(item: dict[str, object], key: str) -> bool | None:
    if key not in item or item[key] is None:
        return None
    value = item[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise TypeError(f"{key} must be a boolean when provided")


def _required_bool(item: dict[str, object], key: str) -> bool:
    value = _optional_bool(item, key)
    if value is None:
        raise TypeError(f"{key} is required")
    return value


def _optional_str(item: dict[str, object], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_str_tuple(item: dict[str, object], key: str) -> tuple[str, ...]:
    value = item.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, list):
        return tuple(text for raw in value if (text := str(raw).strip()))
    raise TypeError(f"{key} must be a string or list when provided")


def _optional_decimal(item: dict[str, object], key: str) -> Decimal | None:
    value = item.get(key)
    if value is None:
        return None
    return Decimal(str(value))


def _provider_datetime(value: object) -> datetime:
    if isinstance(value, bool):
        raise ValueError("provider timestamp must not be boolean")
    if isinstance(value, (int, float)):
        return _epoch_datetime(float(value))
    text = str(value).strip()
    try:
        if text and _is_numeric_timestamp(text):
            return _epoch_datetime(float(text))
        iso_value = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        parsed = datetime.fromisoformat(iso_value)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("invalid provider timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_numeric_timestamp(value: str) -> bool:
    try:
        number = float(value)
    except ValueError:
        return False
    return isfinite(number)


def _epoch_datetime(value: float) -> datetime:
    if not isfinite(value):
        raise ValueError("provider timestamp must be finite")
    # eToro payloads may encode Unix time in seconds or milliseconds.
    seconds = value / 1000 if abs(value) >= 1_000_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("provider timestamp is out of range") from exc


def _market_status(item: dict[str, object]) -> MarketStatus:
    values = tuple(
        value
        for value in (
            _optional_bool(item, "isOpen"),
            _optional_bool(item, "isExchangeOpen"),
        )
        if value is not None
    )
    if any(values):
        return MarketStatus.OPEN
    if values:
        return MarketStatus.CLOSED
    return MarketStatus.UNKNOWN


def _structural_support(
    *,
    delisted: bool | None,
    hidden: bool | None,
) -> tuple[bool, str]:
    if delisted is True:
        return False, "DELISTED"
    if hidden is True:
        return False, "HIDDEN_FROM_CLIENT"
    return True, "SUPPORTED"


def map_demo_eligibility(raw: object, instrument_id: int, symbol: str) -> DemoEligibility:
    if not isinstance(raw, dict):
        raise EtoroMappingError("eligibility payload must be an object")
    try:
        if instrument_id in {int(value) for value in raw.get("notFoundInstrumentIds", [])}:
            raise EtoroMappingError("instrument is not known to Demo eligibility")
        items = raw["eligibilities"]
        item = next(value for value in items if int(value["instrumentId"]) == instrument_id)
        allow_open = _required_bool(item, "allowOpenPosition")
        if not allow_open:
            raise EtoroEligibilityDenied("broker explicitly disallows opening this instrument")
        configs = item["leverageConfigs"]
        config = next(
            value
            for value in configs
            if str(value["settlementType"]).casefold() == "real"
            and str(value["direction"]).casefold() == "long"
            and 1 in {int(leverage) for leverage in value["leverageValues"]}
        )
        minimums = [
            Decimal(str(item["minPositionExposure"])),
            Decimal(str(config["minPositionAmount"])),
        ]
        minimum = max(minimums)
        return DemoEligibility(
            instrument_id=instrument_id,
            symbol=symbol,
            currency=Currency(str(raw["currency"]).upper()),
            minimum_position=minimum,
            allow_open=allow_open,
            allow_close=_optional_bool(item, "allowClosePosition"),
            max_units_per_order=_optional_decimal(item, "maxUnitsPerOrder"),
            allowed_order_quantity_types=_optional_str_tuple(item, "allowedOrderQuantityType"),
            settlement_type=SettlementType.REAL,
            leverage=1,
            verified=True,
        )
    except EtoroMappingError:
        raise
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise EtoroMappingError("complete real-asset Demo eligibility is unavailable") from exc


def map_demo_order_state(raw: object, instrument_id: int, order_id: str) -> ExecutionState:
    if not isinstance(raw, dict):
        raise EtoroMappingError("instrument breakdown payload must be an object")
    try:
        instruments = raw["instruments"]
        instrument = next(
            value for value in instruments if int(value["instrumentId"]) == instrument_id
        )
        order = next(value for value in instrument["orders"] if str(value["orderId"]) == order_id)
        normalized = str(order["status"]).replace("_", "").casefold()
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise EtoroMappingError("Demo order is unavailable for reconciliation") from exc
    states = {
        "received": ExecutionState.SUBMITTED,
        "submitted": ExecutionState.SUBMITTED,
        "pending": ExecutionState.PENDING,
        "filled": ExecutionState.FILLED,
        "partiallyfilled": ExecutionState.PARTIALLY_FILLED,
        "rejected": ExecutionState.REJECTED,
        "cancelled": ExecutionState.CANCELLED,
        "canceled": ExecutionState.CANCELLED,
    }
    return states.get(normalized, ExecutionState.UNKNOWN)


def map_demo_order_lookup_state(raw: object, order_id: str) -> ExecutionState:
    """Map the authoritative eToro order lookup response to a lifecycle state.

    The lookup endpoint is the broker's order-level source of truth.  Keep this
    mapper deliberately fail-closed: an omitted or unfamiliar status must not
    be interpreted as a rejection or a fill.
    """
    if not isinstance(raw, dict):
        raise EtoroMappingError("Demo order lookup payload must be an object")
    returned_order_id = raw.get("orderId")
    if order_id and returned_order_id is not None and str(returned_order_id) != str(order_id):
        raise EtoroMappingError("Demo order lookup returned a different order")
    status = raw.get("status")
    if isinstance(status, dict):
        status_value = status.get("name")
    else:
        status_value = status
    if not isinstance(status_value, str):
        return ExecutionState.UNKNOWN
    normalized = status_value.replace("_", "").replace("-", "").replace(" ", "").casefold()
    states = {
        "received": ExecutionState.SUBMITTED,
        "submitted": ExecutionState.SUBMITTED,
        "accepted": ExecutionState.SUBMITTED,
        "processing": ExecutionState.PENDING,
        "pending": ExecutionState.PENDING,
        "partiallyfilled": ExecutionState.PARTIALLY_FILLED,
        "filled": ExecutionState.FILLED,
        "rejected": ExecutionState.REJECTED,
        "failed": ExecutionState.REJECTED,
        "cancelled": ExecutionState.CANCELLED,
        "canceled": ExecutionState.CANCELLED,
    }
    return states.get(normalized, ExecutionState.UNKNOWN)


def map_quote(raw: object, *, instrument_id: int, symbol: str, currency: Currency) -> MarketQuote:
    if not isinstance(raw, dict):
        raise EtoroMappingError("rate payload must be an object")
    try:
        rows = raw["rates"]
        matching_rows = [
            row
            for row in rows
            if isinstance(row, dict) and int(row["instrumentID"]) == instrument_id
        ]
        if not matching_rows:
            raise StopIteration
        row, as_of = max(
            ((_row, _provider_datetime(_row["date"])) for _row in matching_rows),
            key=lambda item: item[1],
        )
        return MarketQuote(
            instrument_id=instrument_id,
            symbol=symbol,
            price=Decimal(str(row["lastExecution"])),
            bid=Decimal(str(row["bid"])),
            ask=Decimal(str(row["ask"])),
            as_of=as_of,
            currency=currency,
            source="etoro-official-api",
            market_status=MarketStatus.UNKNOWN,
        )
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise EtoroMappingError("invalid rate payload") from exc


def map_portfolio(
    raw: object, *, as_of: datetime, currency: Currency, symbols: dict[int, str]
) -> PortfolioSnapshot:
    if not isinstance(raw, dict):
        raise EtoroMappingError("portfolio payload must be an object")
    try:
        data = raw["clientPortfolio"]
        positions = tuple(
            Position(
                position_id=str(p["positionID"]),
                instrument_id=int(p["instrumentID"]),
                symbol=symbols[int(p["instrumentID"])],
                settlement_type=SettlementType.REAL,
                units=Decimal(str(p["units"])),
                average_entry_price=Decimal(str(p["openRate"])),
                market_price=Decimal(str(p["openRate"])),
            )
            for p in data.get("positions", [])
            if int(p["leverage"]) == 1 and bool(p["isBuy"])
        )
        return PortfolioSnapshot(
            as_of=as_of, currency=currency, cash=Decimal(str(data["credit"])), positions=positions
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EtoroMappingError("invalid portfolio payload") from exc
