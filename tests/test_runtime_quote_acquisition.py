from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest

from app.brokers.etoro.client import EtoroApiError
from app.data.historical.quotes import QuoteObservationStore
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.market import MarketQuote
from app.domain.universe import UniversalInstrument
from app.orchestration.quote_acquisition import observe_runtime_quotes, assess_current_spread

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)


def instruments():
    return tuple(UniversalInstrument(broker='etoro', broker_instrument_id=str(i),symbol=f'T{i}',
        asset_class=AssetClass.CRYPTO,currency=Currency.USD,market_status=MarketStatus.OPEN,
        metadata_timestamp=NOW) for i in range(1,11))


class Client:
    def __init__(self, throttle=False):
        self.calls = []
        self.throttle = throttle

    def quote(self, instrument_id, symbol, *, currency):
        self.calls.append(instrument_id)
        if self.throttle:
            raise EtoroApiError('limit',endpoint='rates',status=429)
        return MarketQuote(instrument_id=instrument_id,symbol=symbol,currency=currency,
            price=Decimal('100'),bid=Decimal('99'),ask=Decimal('101'),source='etoro-official-api',as_of=NOW)


def test_bounded_rotation_and_no_retroactive_join(tmp_path):
    store = QuoteObservationStore(tmp_path/'quotes.sqlite3')
    client = Client()
    args = dict(client=client,store=store,instruments=instruments(),cutoff=NOW,
                clock=lambda:NOW+timedelta(seconds=1))
    result = observe_runtime_quotes(**args)
    assert result['persisted'] == result['attempted'] == 8
    assert result['available_at_cutoff'] == []
    assert all(a['spread_ratio'] is None for a in result['spread_assessments'])
    initial = set(client.calls)
    client.calls.clear()
    result = observe_runtime_quotes(**{**args,'cutoff':NOW+timedelta(seconds=2)})
    assert set(client.calls[:2]).isdisjoint(initial)
    assert len(result['available_at_cutoff']) == 10
    store.close()


@pytest.mark.parametrize('bid,ask,status,bps', [
    ('99','101','WITHIN_PROFILE_LIMIT','200'),
    ('98','102','ABOVE_PROFILE_LIMIT','400'),
    ('98.5','101.5','WITHIN_PROFILE_LIMIT','300'),
])
def test_spread_uses_existing_crypto_profile_without_order_permission(bid,ask,status,bps):
    item = instruments()[0]
    quote = MarketQuote(instrument_id=1,symbol='T1',currency=Currency.USD,price=Decimal('100'),
        bid=Decimal(bid),ask=Decimal(ask),source='etoro-official-api',as_of=NOW)
    result = assess_current_spread(instrument=item,quote=quote,cutoff=NOW,max_age=timedelta(seconds=120))
    assert result['status'] == status
    assert Decimal(result['spread_bps']) == Decimal(bps)
    assert result['profile_spread_limit'] == '0.03'
    assert result['authorizes_order'] is False
    assert result['historical_score_changed'] is False
    result = assess_current_spread(instrument=item,quote=quote,cutoff=NOW-timedelta(seconds=1),max_age=timedelta(seconds=120))
    assert result['status'] == 'QUOTE_INVALID_AT_CUTOFF'


def test_rate_limit_stops_stream_and_survives_reopen(tmp_path):
    path = tmp_path/'quotes.sqlite3'
    store = QuoteObservationStore(path)
    client = Client(throttle=True)
    args = dict(client=client,store=store,instruments=instruments(),cutoff=NOW,clock=lambda:NOW)
    assert observe_runtime_quotes(**args)['attempted'] == 1
    store.close()
    store = QuoteObservationStore(path)
    assert observe_runtime_quotes(**{**args,'store':store})['attempted'] == 0
    assert len(client.calls) == 1
    store.close()
