import logging
import sys
import traceback

from bot.breakers import TRADING_OK, check_circuit_breakers
from bot.config import RISK, UNIVERSE
from bot.data import get_daily_bars
from bot.execute import execute_order, get_equity, get_positions, stop_loss_sweep
from bot.journal import count_trades_today, journal_error, journal_run, read_entries, symbols_in_cooldown
from bot.signals import generate_signal, get_indicators, get_regime

logger = logging.getLogger("bot.main")

SLEEVE = "TACTICAL"
POSITION_SIZE_PCT = RISK["tactical_position_pct"] / 100


def _pregate(status, symbol, action, usd_amount, sleeve, reason):
    logger.info("%s %s %s $%.2f (%s): %s", status, action, symbol, usd_amount, sleeve, reason)
    return {
        "status": status,
        "symbol": symbol,
        "action": action,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "reason": reason,
        "quote": None,
    }


def _run():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]

    journal_entries = read_entries()
    equity = get_equity()
    breaker_status = check_circuit_breakers(journal_entries, equity)
    if breaker_status != TRADING_OK:
        logger.warning("Circuit breaker active: %s — BUYs will be rejected this run.", breaker_status)

    # Stop-losses run unconditionally, breaker or not: they only reduce risk.
    results = stop_loss_sweep()

    held_symbols = {p.symbol for p in get_positions()}
    trades_today_count = count_trades_today(journal_entries, action="BUY")
    cooldown_symbols = symbols_in_cooldown(journal_entries, RISK["ticker_cooldown_days"], action="BUY")

    # Sized off live account equity, not a hardcoded dollar figure, so this
    # tracks deposits/withdrawals/resets on the paper account automatically.
    buy_usd_amount = equity * POSITION_SIZE_PCT

    bars = get_daily_bars(symbols, days=250)

    indicators = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    print(f"Regime: {regime}  Breaker: {breaker_status}\n")
    header = (
        f"{'Symbol':<8}{'Close':>10}{'EMA9':>10}{'EMA21':>10}{'EMA50':>10}"
        f"{'EMA200':>10}{'%vsEMA200':>11}{'RSI14':>8}{'Cross':>8}{'Signal':>8}"
    )
    print(header)
    print("-" * len(header))

    analyzed = {}
    for symbol in symbols:
        ind = indicators[symbol]
        signal = generate_signal(ind, regime)
        analyzed[symbol] = {**ind, "signal": signal}
        print(
            f"{symbol:<8}{ind['close']:>10.2f}{ind['ema9']:>10.2f}{ind['ema21']:>10.2f}"
            f"{ind['ema50']:>10.2f}{ind['ema200']:>10.2f}{ind['pct_from_ema200']:>10.1f}%"
            f"{ind['rsi14']:>8.1f}{ind['cross']:>8}{signal:>8}"
        )

        if signal == "BUY":
            if symbol in held_symbols:
                results.append(
                    _pregate(
                        "SKIPPED_ALREADY_HELD",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        "already holding a position in this ticker",
                    )
                )
            elif breaker_status != TRADING_OK:
                results.append(
                    _pregate(
                        "REJECTED",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        f"BREAKER_DEFENSIVE ({breaker_status})",
                    )
                )
            elif symbol in cooldown_symbols:
                results.append(
                    _pregate(
                        "SKIPPED_COOLDOWN",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        f"bought within the last {RISK['ticker_cooldown_days']}d cooldown window",
                    )
                )
            elif trades_today_count >= RISK["max_trades_per_day"]:
                results.append(
                    _pregate(
                        "SKIPPED_TRADE_LIMIT",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        f"daily trade limit reached ({RISK['max_trades_per_day']})",
                    )
                )
            else:
                result = execute_order(symbol, "BUY", buy_usd_amount, SLEEVE)
                results.append(result)
                if result["status"] == "SUBMITTED":
                    trades_today_count += 1
        elif signal == "SELL" and symbol in held_symbols:
            position = next(p for p in get_positions() if p.symbol == symbol)
            results.append(execute_order(symbol, "SELL", float(position.market_value), SLEEVE))

    print("\nExecution summary:")
    if not results:
        print("  no actions taken")
    for r in results:
        if r["status"] == "SUBMITTED":
            size = f"notional=${r['notional']:.2f}" if r["notional"] is not None else f"qty={r['qty']}"
            print(
                f"  {r['symbol']:<6} {r['action']:<4} SUBMITTED {size} "
                f"bid={r['quote']['bid']:.2f} ask={r['quote']['ask']:.2f} order_id={r['order_id']}"
            )
        elif r["status"].startswith("SKIPPED_"):
            print(f"  {r['symbol']:<6} {r['action']:<4} {r['status']} — {r['reason']}")
        else:
            print(f"  {r['symbol']:<6} {r['action']:<4} REJECTED — {r['reason']}")

    final_positions = get_positions()
    final_equity = get_equity()
    journal_run(regime, analyzed, results, final_positions, final_equity, breaker_status)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        _run()
    except Exception as exc:
        journal_error(exc)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
