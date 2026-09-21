from datetime import UTC, datetime, timedelta
from app.brokers.models import InstrumentResolution
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import enrich_instrument_session_state
from app.storage.sqlite import SqliteRecordStore


def test_renewal_before_expiry_is_bounded_and_uses_nearest_expiry(tmp_path):
    now = datetime(2026,9,14,7,tzinfo=UTC)
    items = tuple(UniversalInstrument(broker='etoro',broker_instrument_id=str(i),symbol=s,
        asset_class=AssetClass.EQUITY,currency=Currency.USD,metadata_timestamp=now)
        for i,s in ((1,'ZZZ'),(2,'AAA')))
    store = SqliteRecordStore(tmp_path/'sessions.sqlite3')
    calls = []
    class Client:
        def resolve_instrument_id(self, instrument_id, *, symbol, as_of):
            calls.append(symbol)
            return InstrumentResolution(instrument_id=instrument_id,symbol=symbol,internal_symbol_full=symbol,
                market_status=MarketStatus.CLOSED,is_currently_tradable=True,is_buy_enabled=True,
                is_hidden_from_client=False,is_delisted=False,is_active_in_platform=True,
                resolved=True,structurally_supported=True,structural_status='SUPPORTED',verified=True,as_of=as_of)
    client = Client()
    for index,item in enumerate(items):
        enrich_instrument_session_state(client=client,store=store,instruments=(item,),as_of=now+timedelta(seconds=index*30))
    calls.clear()
    result = enrich_instrument_session_state(client=client,store=store,instruments=items,
        as_of=now+timedelta(minutes=14),refresh_ahead=timedelta(seconds=120),batch_size=1)
    assert calls == ['ZZZ']
    assert all(i.market_status is MarketStatus.CLOSED for i in result)
    states = store.etoro_session_states(as_of=now+timedelta(minutes=14))
    assert states['2']['expires_at'] == (now+timedelta(minutes=15,seconds=30)).isoformat()
