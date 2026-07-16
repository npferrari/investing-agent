import logging
import math

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from bot.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, SECTOR_MAP, UNIVERSE
from bot.data import get_latest_quote

logger = logging.getLogger("bot.execute")

_trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

_ALL_SYMBOLS = set(UNIVERSE["STOCKS"]) | set(UNIVERSE["ETFS"]) | set(UNIVERSE["HEDGE"])

MAX_OPEN_POSITIONS = 14
MAX_PCT_PER_TICKER = 0.12
MAX_PCT_PER_SECTOR = 0.25

# Every position is currently attributed to this sleeve — execute_order
# doesn't yet support issuing BUYs under any other sleeve, and there is no
# persisted per-position sleeve store, so a mixed-sleeve book can't yet exist.
DEFAULT_SLEEVE = "TACTICAL"


def get_positions():
    return _trading_client.get_all_positions()


def get_equity():
    return float(_trading_client.get_account().equity)


def _reject(symbol, action, usd_amount, sleeve, reason, quote=None):
    logger.warning("REJECTED %s %s $%.2f (%s): %s", action, symbol, usd_amount, sleeve, reason)
    return {
        "status": "REJECTED",
        "symbol": symbol,
        "action": action,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "reason": reason,
        "quote": quote,
    }


def execute_order(symbol, action, usd_amount, sleeve):
    action = action.upper()

    if symbol not in _ALL_SYMBOLS:
        return _reject(symbol, action, usd_amount, sleeve, f"{symbol} is outside UNIVERSE")

    clock = _trading_client.get_clock()
    if not clock.is_open:
        return _reject(symbol, action, usd_amount, sleeve, "market is closed")

    # Quote is fetched and logged for every order attempt that reaches here,
    # independent of accept/reject, so spread + slippage vs. this reference
    # price can be reconstructed later from the fill.
    quote = get_latest_quote(symbol)[symbol]
    quote_log = {"bid": quote.bid_price, "ask": quote.ask_price, "timestamp": quote.timestamp}

    price = quote.ask_price if action == "BUY" else quote.bid_price
    if not price or price <= 0:
        return _reject(symbol, action, usd_amount, sleeve, "no valid quote available", quote_log)

    qty = math.floor(usd_amount / price)
    if qty < 1:
        return _reject(
            symbol,
            action,
            usd_amount,
            sleeve,
            f"${usd_amount:.2f} buys less than one whole share at ${price:.2f}",
            quote_log,
        )

    positions = _trading_client.get_all_positions()
    held_symbols = {p.symbol for p in positions}

    if action == "BUY":
        account = _trading_client.get_account()
        equity = float(account.equity)
        order_value = qty * price

        if symbol not in held_symbols and len(positions) >= MAX_OPEN_POSITIONS:
            return _reject(
                symbol,
                action,
                usd_amount,
                sleeve,
                f"would exceed max open positions ({MAX_OPEN_POSITIONS})",
                quote_log,
            )

        existing_ticker_value = sum(float(p.market_value) for p in positions if p.symbol == symbol)
        ticker_value_after = existing_ticker_value + order_value
        if ticker_value_after > MAX_PCT_PER_TICKER * equity:
            return _reject(
                symbol,
                action,
                usd_amount,
                sleeve,
                f"would exceed {MAX_PCT_PER_TICKER:.0%} equity cap per ticker "
                f"(${ticker_value_after:.2f} / ${equity:.2f})",
                quote_log,
            )

        sector = SECTOR_MAP.get(symbol)
        if sector is not None:
            sector_value = sum(
                float(p.market_value) for p in positions if SECTOR_MAP.get(p.symbol) == sector
            )
            sector_value_after = sector_value + order_value
            if sector_value_after > MAX_PCT_PER_SECTOR * equity:
                return _reject(
                    symbol,
                    action,
                    usd_amount,
                    sleeve,
                    f"would exceed {MAX_PCT_PER_SECTOR:.0%} equity cap for sector "
                    f"'{sector}' (${sector_value_after:.2f} / ${equity:.2f})",
                    quote_log,
                )

    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = _trading_client.submit_order(order_request)

    logger.info(
        "SUBMITTED %s %s qty=%d (~$%.2f, sleeve=%s) bid=%.2f ask=%.2f order_id=%s",
        action,
        symbol,
        qty,
        qty * price,
        sleeve,
        quote.bid_price,
        quote.ask_price,
        order.id,
    )

    return {
        "status": "SUBMITTED",
        "symbol": symbol,
        "action": action,
        "qty": qty,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "quote": quote_log,
        "order_id": str(order.id),
    }
