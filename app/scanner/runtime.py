"""Runtime composition for the read-only universal market scanner."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.runtime import runtime_credentials
from app.brokers.etoro.scanner_adapter import EtoroMarketScannerAdapter
from app.brokers.models import DemoPortfolioSnapshot
from app.config.models import ApplicationConfig
from app.domain.enums import SettlementType
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.versions import RANKING_VERSION
from app.policies.defaults import default_asset_policy_engine
from app.scanner.models import ScannerLimits
from app.scanner.ranking import OpportunityRankingEngine
from app.scanner.service import OpenMarketCandidateScanner
from app.storage.sqlite import SqliteRecordStore

DEFAULT_MARKET_SCAN_STORE_PATH = Path("work") / "market-scan.sqlite3"


def default_market_scan_store(path: Path | None = None) -> SqliteRecordStore:
    return SqliteRecordStore(path or DEFAULT_MARKET_SCAN_STORE_PATH)


def build_market_scan_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    store: SqliteRecordStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    credentials = runtime_credentials(values)
    if credentials is None:
        return _blocked_payload(
            "NOT_CONFIGURED",
            "CREDENTIALS",
            "eToro credentials are not configured",
            config=config,
        )
    if not config.etoro_api_enabled:
        return _blocked_payload(
            "NOT_CONFIGURED",
            "API_DISABLED",
            "ETORO_API_ENABLED is false",
            config=config,
        )
    if config.etoro_demo_execution_enabled:
        return _blocked_payload(
            "BLOCKED",
            "DEMO_EXECUTION_ENABLED",
            "ETORO_DEMO_EXECUTION_ENABLED must remain false for scan-markets",
            config=config,
        )
    read_client = client or EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    try:
        identity = read_client.identity()
        adapter = EtoroMarketScannerAdapter(
            read_client,
            search_text=config.scanner.etoro_search_text,
            max_pages=config.scanner.etoro_max_pages,
        )
        demo = read_client.demo_account(identity)
        portfolio = _portfolio_from_demo(demo, symbols={})
        scanner = OpenMarketCandidateScanner(
            adapter=adapter,
            policy_engine=default_asset_policy_engine(),
            ranking_engine=OpportunityRankingEngine(ranking_version=RANKING_VERSION),
            limits=ScannerLimits(
                discovery_limit=config.scanner.discovery_limit,
                ranked_shortlist_limit=config.scanner.ranked_shortlist_limit,
                deep_analysis_limit=config.scanner.deep_analysis_limit,
            ),
            store=store,
        )
        result = scanner.scan(portfolio=portfolio, as_of=now)
        if result.total_discovered == 0:
            return _blocked_payload(
                "BLOCKED",
                "NO_INSTRUMENTS_DISCOVERED",
                "eToro discovery returned no normalized instruments",
                config=config,
            )
    except EtoroApiError as exc:
        metadata = exc.safe_metadata()
        return {
            **_blocked_payload(
                "BLOCKED",
                metadata.get("category", "ETORO_API_ERROR"),
                "read-only eToro market scan failed",
                config=config,
            ),
            "endpoint": metadata.get("endpoint"),
            "http_status": metadata.get("http_status"),
            "transport_detail": metadata.get("transport_detail"),
            "cf_ray": metadata.get("cf_ray"),
            "content_type": metadata.get("content_type"),
        }
    except (RuntimeError, ValueError) as exc:
        return _blocked_payload(
            "BLOCKED",
            type(exc).__name__,
            "read-only market scan could not complete",
            config=config,
        )

    return {
        "status": "LIVE_VERIFIED",
        "broker": result.broker,
        "record_store_path": str(DEFAULT_MARKET_SCAN_STORE_PATH),
        "markets_scanned": result.total_discovered,
        "asset_classes_found": tuple(item.value for item in result.asset_classes_found),
        "open_markets": result.open_markets,
        "closed_markets": result.closed_markets,
        "policy_blocked_markets": result.policy_blocked,
        "broker_eligible_instruments": result.broker_eligible,
        "candidates_ranked": len(result.ranked_candidates),
        "top_candidates": tuple(
            {
                "rank": item.rank,
                "symbol": item.instrument.symbol,
                "asset_class": item.asset_class.value,
                "market_status": item.market_status.value,
                "state": item.candidate_state.value,
                "score": str(item.candidate_score),
                "data_quality": item.data_quality.value,
            }
            for item in result.ranked_candidates[: config.scanner.deep_analysis_limit]
        ),
        "broker_write_calls": result.broker_write_calls,
        "demo_execution_enabled": result.demo_execution_enabled,
        "real_execution_available": result.real_execution_available,
    }


def _portfolio_from_demo(
    snapshot: DemoPortfolioSnapshot, *, symbols: Mapping[int, str]
) -> PortfolioSnapshot:
    positions = tuple(
        Position(
            position_id=f"demo-position-{position.instrument_id}",
            instrument_id=position.instrument_id,
            symbol=symbols.get(position.instrument_id, f"instrument-{position.instrument_id}"),
            settlement_type=SettlementType.REAL,
            units=position.units,
            average_entry_price=position.average_open_rate,
            market_price=(
                position.current_exposure / position.units
                if position.units > 0
                else position.average_open_rate
            ),
        )
        for position in snapshot.positions
        if position.units > 0
    )
    return PortfolioSnapshot(
        as_of=snapshot.as_of,
        currency=snapshot.currency,
        cash=snapshot.cash,
        positions=positions,
        reported_total_value=snapshot.total_value,
        peak_value=max(snapshot.total_value, snapshot.account_balance)
        if snapshot.account_balance > 0
        else snapshot.total_value,
    )


def _blocked_payload(
    status: str, category: str, reason: str, *, config: ApplicationConfig
) -> dict[str, object]:
    return {
        "status": status,
        "category": category,
        "reason": reason,
        "record_store_path": str(DEFAULT_MARKET_SCAN_STORE_PATH),
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }
