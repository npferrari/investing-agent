from datetime import datetime, timedelta, timezone

from alpaca.data.enums import Adjustment
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from bot.config import ALPACA_API_KEY, ALPACA_SECRET_KEY

_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# Adjustment.ALL is mandatory: unadjusted bars carry raw split/dividend price
# jumps (e.g. a 4:1 split shows as an overnight -75% "close"), which corrupt
# every rolling indicator (EMA, RSI) computed across the event.
_ADJUSTMENT = Adjustment.ALL


def get_daily_bars(symbols, days):
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days),
        adjustment=_ADJUSTMENT,
    )
    return _client.get_stock_bars(request).df


def get_intraday_bars(symbols, minutes):
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=datetime.now(timezone.utc) - timedelta(minutes=minutes),
        adjustment=_ADJUSTMENT,
    )
    return _client.get_stock_bars(request).df


def get_latest_quote(symbols):
    request = StockLatestQuoteRequest(symbol_or_symbols=symbols)
    return _client.get_stock_latest_quote(request)
