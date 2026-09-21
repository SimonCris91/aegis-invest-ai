from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.historical.quotes import QuoteObservationStore
from app.domain.enums import Currency
from app.domain.market import MarketQuote

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)


@pytest.mark.parametrize('provider_offset,received_offset,expected', [
    (-10,-5,True), (-10,1,False), (1,2,False), (-121,-5,False), (0,-5,False),
])
def test_causal_selection(tmp_path, provider_offset, received_offset, expected):
    store = QuoteObservationStore(tmp_path/'quotes.sqlite3')
    quote = MarketQuote(instrument_id=100000,symbol='BTC',price=Decimal('100'),
        bid=Decimal('99'),ask=Decimal('101'),currency=Currency.USD,source='etoro-official-api',
        as_of=NOW+timedelta(seconds=provider_offset))
    store.record(provider='etoro',quote=quote,received_at=NOW+timedelta(seconds=received_offset))
    args = dict(provider='etoro',instrument_id=100000,symbol='BTC',currency=Currency.USD,
        cutoff=NOW.astimezone(timezone(timedelta(hours=2))),max_age=timedelta(seconds=120))
    assert (store.at_cutoff(**args) is not None) == expected
    for override in ({'symbol':'ETH'}, {'instrument_id':100001}, {'provider':'other'}, {'currency':Currency.EUR}):
        assert store.at_cutoff(**{**args,**override}) is None
    store.close()


def test_rejects_naive_cutoff_and_invalid_age(tmp_path):
    store = QuoteObservationStore(tmp_path/'quotes.sqlite3')
    args = dict(provider='etoro',instrument_id=1,symbol='X',currency=Currency.USD,
                cutoff=NOW,max_age=timedelta(seconds=120))
    with pytest.raises(ValueError):
        store.at_cutoff(**{**args,'cutoff':NOW.replace(tzinfo=None)})
    with pytest.raises(ValueError):
        store.at_cutoff(**{**args,'max_age':timedelta(0)})
    store.close()
