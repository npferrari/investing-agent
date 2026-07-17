import logging
import math

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from bot.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, DEFAULT_SLEEVE, RISK, SECTOR_MAP, UNIVERSE
from bot.data import get_latest_quote
from bot.journal import get_ramp_cap_pct, read_entries

logger = logging.getLogger("bot.execute")

_trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

_ALL_SYMBOLS = set(UNIVERSE["STOCKS"]) | set(UNIVERSE["ETFS"]) | set(UNIVERSE["HEDGE"])

MAX_OPEN_POSITIONS = RISK["max_positions"]
MAX_PCT_PER_TICKER = RISK["max_ticker_pct"] / 100
MAX_PCT_PER_SECTOR = RISK["max_sector_pct"] / 100


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

    # Prefer a notional (dollar-denominated) market order so position size
    # tracks usd_amount exactly. Alpaca rejects notional orders for
    # non-fractionable instruments, so fall back to a whole-share qty order
    # for those.
    asset = _trading_client.get_asset(symbol)
    notional = None
    qty = None
    if asset.fractionable:
        notional = round(usd_amount, 2)
        order_value = notional
    else:
        qty = math.floor(usd_amount / price)
        if qty < 1:
            return _reject(
                symbol,
                action,
                usd_amount,
                sleeve,
                f"{symbol} is not fractionable and ${usd_amount:.2f} buys less than "
                f"one whole share at ${price:.2f}",
                quote_log,
            )
        order_value = qty * price

    positions = _trading_client.get_all_positions()
    held_symbols = {p.symbol for p in positions}

    if action == "BUY":
        # Caps are checked against live account equity fetched fresh here,
        # never a hardcoded balance, since the paper account's equity can
        # change (deposits, resets, prior fills) between runs.
        equity = get_equity()

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

        # deploy_ramp caps total invested % of equity by account-age week,
        # so a young account can't go all-in immediately. Cap is read fresh
        # from journal history every call, not cached, since account age
        # advances and week boundaries roll over between runs.
        ramp_cap_pct = get_ramp_cap_pct(read_entries())
        invested_value = sum(float(p.market_value) for p in positions)
        invested_after = invested_value + order_value
        if invested_after > (ramp_cap_pct / 100) * equity:
            return _reject(
                symbol,
                action,
                usd_amount,
                sleeve,
                f"RAMP_LIMIT: would push total invested to ${invested_after:.2f} "
                f"(cap is {ramp_cap_pct}% of ${equity:.2f} equity)",
                quote_log,
            )

    # notional and qty are mutually exclusive on MarketOrderRequest; exactly
    # one is set above depending on whether the asset is fractionable.
    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        notional=notional,
        side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = _trading_client.submit_order(order_request)

    logger.info(
        "SUBMITTED %s %s %s (~$%.2f, sleeve=%s) bid=%.2f ask=%.2f order_id=%s",
        action,
        symbol,
        f"notional=${notional:.2f}" if notional is not None else f"qty={qty}",
        order_value,
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
        "notional": notional,
        "order_value": order_value,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "quote": quote_log,
        "order_id": str(order.id),
    }


def stop_loss_sweep():
    """SELL any open position breaching its sleeve's stop-loss threshold.

    Runs every cycle unconditionally, including when circuit breakers have
    the account in DEFENSIVE MODE — a stop-loss is risk-reducing, so it must
    never be blocked by a freeze that only gates new BUYs.
    """
    results = []
    for position in _trading_client.get_all_positions():
        sleeve = DEFAULT_SLEEVE
        stop_pct = RISK[f"stop_{sleeve.lower()}_pct"]
        unrealized_pct = float(position.unrealized_plpc) * 100
        if unrealized_pct > stop_pct:
            continue

        logger.warning(
            "STOP_LOSS %s unrealized=%.2f%% <= stop=%.2f%% (sleeve=%s) — selling",
            position.symbol,
            unrealized_pct,
            stop_pct,
            sleeve,
        )
        # Sell the full position: usd_amount is its current market value, so
        # the notional/qty logic above closes it out rather than trimming it.
        results.append(execute_order(position.symbol, "SELL", float(position.market_value), sleeve))
    return results
