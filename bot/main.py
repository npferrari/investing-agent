import argparse
import logging
import sys
import traceback

from alpaca.trading.enums import TimeInForce

from bot import brain
from bot.breakers import TRADING_OK, check_circuit_breakers
from bot.briefing import build_briefing
from bot.config import HEADLINES_MACRO_CAP, HEADLINES_PER_TICKER_CAP, SLIPPAGE_HAIRCUT_PCT, UNIVERSE
from bot.data import get_daily_bars
from bot.execute import execute_order, get_equity, get_positions_with_sleeves, stop_loss_sweep
from bot.journal import (
    count_risk_adding_trades,
    in_construction_window,
    journal_brain_run,
    journal_error,
    journal_token_usage,
    read_entries,
)
from bot.news import get_macro_headlines, get_ticker_headlines
from bot.risk import validate_proposals
from bot.screener import select_candidates
from bot.signals import generate_signal, get_indicators, get_regime

logger = logging.getLogger("bot.main")

# EMA200 needs ~2-3x its period of history to converge (see signals.py's step-2
# rationale), and the briefing's 52-week percentile needs ~252 trading days.
FULL_RUN_BAR_DAYS = 400

# Order of execution within one run: SELLs/TRIMs before BUYs, matching
# risk.py's own validation order — this is what makes a swap at the holdings
# cap actually work end to end (the SELL's cash/slot freed before the BUY
# submits), not just validate correctly on paper.
_ACTION_ORDER = {"SELL": 0, "TRIM": 0, "BUY": 1, "HOLD": 2}


def _sleeve_utilization(positions, equity):
    """`positions` are execute.get_positions_with_sleeves()'s resolved
    dicts — sleeve already resolved once, upstream, via
    positions_state.reconcile(). No independent lookup here."""
    invested = {"MOMENTUM": 0.0, "QUALITY_VALUE": 0.0, "DEFENSIVE": 0.0}
    for p in positions:
        invested[p["sleeve"]] = invested.get(p["sleeve"], 0.0) + p["market_value"]
    if not equity:
        return {sleeve: 0.0 for sleeve in invested}
    return {sleeve: value / equity * 100 for sleeve, value in invested.items()}


def _risk_state(equity, positions, regime, breaker_status, run_mode, validated_symbols):
    """`positions` are already execute.get_positions_with_sleeves()'s
    resolved dicts — they already carry symbol/qty/market_value/sleeve,
    which is everything risk.py's state["positions"] needs; extra keys
    (avg_entry_price, opened_at, ...) are harmless passengers it ignores."""
    entries = read_entries()
    return {
        "equity": equity,
        "positions": positions,
        "regime": regime,
        "breaker_status": breaker_status,
        "run_mode": run_mode,
        # G13's real NORMAL/DANGER state machine with dwell timers doesn't
        # exist yet (bot/regime.py — research/v1-architecture.md §4.5), so
        # there is no real dwell state to compute. Always True until that
        # module exists; documented here rather than silently assumed.
        "dwell_ok": True,
        "in_construction_window": in_construction_window(entries),
        "risk_adding_trades_this_week": count_risk_adding_trades(entries),
        "validated_symbols": validated_symbols,
    }


def _flatten_proposals(candidates):
    return [{**c["proposal"], "symbol": c["symbol"]} for c in candidates]


def _with_slippage_estimate(execution):
    if execution is None or execution["status"] != "SUBMITTED":
        return execution
    haircut = execution["order_value"] * SLIPPAGE_HAIRCUT_PCT / 100
    return {
        **execution,
        "slippage_haircut_pct": SLIPPAGE_HAIRCUT_PCT,
        "estimated_slippage_cost_usd": haircut,
    }


def _validate_and_execute(candidates, risk_state, run_mode):
    """risk.py verdicts, then execution for anything APPROVED and actionable.

    Returns candidates enriched with "verdict" and "execution", in
    SELL/TRIM-before-BUY order (matching risk.py's own internal ordering),
    so a swap at the holdings cap actually settles in the right sequence.
    """
    proposals = _flatten_proposals(candidates)
    verdicts_by_symbol = {v["symbol"]: v for v in validate_proposals(proposals, risk_state)}

    time_in_force = TimeInForce.OPG if run_mode == "FULL" else TimeInForce.DAY

    ordered = sorted(candidates, key=lambda c: _ACTION_ORDER.get(c["proposal"]["action"], 2))
    enriched = []
    for candidate in ordered:
        symbol = candidate["symbol"]
        verdict = verdicts_by_symbol[symbol]
        execution = None
        if verdict["status"] == "APPROVED" and verdict["action"] != "HOLD":
            execution = execute_order(symbol, verdict["action"], verdict["usd_amount"], verdict["sleeve"], time_in_force=time_in_force)
            execution = _with_slippage_estimate(execution)
        enriched.append(
            {
                **candidate,
                "verdict": {"status": verdict["status"], "guardrail": verdict["guardrail"], "reason": verdict["reason"]},
                "execution": execution,
            }
        )
    return enriched


def _print_verdicts(candidates):
    print("\nRisk verdicts:")
    for c in candidates:
        verdict = c["verdict"]
        proposal = c["proposal"]
        if verdict["status"] == "APPROVED":
            print(f"  {c['symbol']:<6} {proposal['action']:<4} APPROVED")
        else:
            print(f"  {c['symbol']:<6} {proposal['action']:<4} REJECTED [{verdict['guardrail']}] {verdict['reason']}")


def _run_light():
    entries = read_entries()
    equity = get_equity()
    breaker_status = check_circuit_breakers(entries, equity)
    if breaker_status != TRADING_OK:
        logger.warning("Circuit breaker active: %s — BUYs will be rejected this run.", breaker_status)

    # Stop-losses run unconditionally, breaker or not — they only reduce risk.
    sweep_results = stop_loss_sweep()

    positions = get_positions_with_sleeves()
    held_symbols = sorted({p["symbol"] for p in positions})

    # Bars are fetched for the whole universe, not just held symbols + SPY/QQQ
    # — briefing._neighborhood() needs each held symbol's sector ETF and peer
    # stocks too (a held position can be in any sector), and bars are free,
    # code-side data (T1) that never reaches the LLM, so there's no token
    # cost to fetching more than strictly needed. Only the *candidates* list
    # (held-only) is what actually keeps a light run cheap and small.
    all_symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]
    bars = get_daily_bars(all_symbols, days=FULL_RUN_BAR_DAYS)
    universe_data = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    if not held_symbols:
        logger.info("LIGHT run: no held positions — nothing to monitor.")
        journal_brain_run("LIGHT", regime, breaker_status, [], sweep_results, positions, equity, {})
        return

    # Held positions only — no discovery candidates. Light runs never open
    # new positions, so briefing the discovery universe would be pure waste
    # (§5 token economy) and, per the 2026-07-24 build review, actively hurts
    # coverage on the smaller model by diluting its attention across
    # candidates it can never act on anyway.
    ticker_headlines = get_ticker_headlines(held_symbols, cap=HEADLINES_PER_TICKER_CAP)
    macro_headlines = get_macro_headlines(cap=HEADLINES_MACRO_CAP)
    sleeve_utilization = _sleeve_utilization(positions, equity)

    briefing = build_briefing(
        held_symbols,
        universe_data,
        bars,
        positions,
        ticker_headlines,
        macro_headlines,
        regime,
        breaker_status,
        sleeve_utilization,
        equity,
    )

    result = brain.run_light_analysis(briefing, held_symbols)
    if result["usage"] is not None:
        journal_token_usage("LIGHT", result["model"], result["section_estimates"], result["usage"])

    candidates = result["candidates"] or []
    risk_state = _risk_state(equity, positions, regime, breaker_status, "LIGHT", set(universe_data.keys()))
    enriched = _validate_and_execute(candidates, risk_state, "LIGHT")

    final_positions = get_positions_with_sleeves()
    final_equity = get_equity()
    journal_brain_run(
        "LIGHT", regime, breaker_status, enriched, sweep_results, final_positions, final_equity, result.get("usage") or {}
    )


def _run_full():
    entries = read_entries()
    equity = get_equity()
    breaker_status = check_circuit_breakers(entries, equity)
    if breaker_status != TRADING_OK:
        logger.warning("Circuit breaker active: %s — BUYs will be rejected this run.", breaker_status)

    sweep_results = stop_loss_sweep()

    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]
    bars = get_daily_bars(symbols, days=FULL_RUN_BAR_DAYS)
    universe_data = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    # v0's signal is computed and journaled as a benchmark only — it never
    # executes. The screener/briefing/brain/risk pipeline below is the sole
    # path that trades for real (step 13 retires v0 execution).
    v0_signals = {symbol: generate_signal(universe_data[symbol], regime) for symbol in symbols}

    positions = get_positions_with_sleeves()
    ticker_headlines = get_ticker_headlines(symbols, cap=HEADLINES_PER_TICKER_CAP)
    macro_headlines = get_macro_headlines(cap=HEADLINES_MACRO_CAP)

    candidates_list, scores = select_candidates(universe_data, positions, ticker_headlines)
    sleeve_utilization = _sleeve_utilization(positions, equity)

    briefing = build_briefing(
        candidates_list,
        universe_data,
        bars,
        positions,
        ticker_headlines,
        macro_headlines,
        regime,
        breaker_status,
        sleeve_utilization,
        equity,
    )

    full_result, shadow_result = brain.run_full_analysis(briefing, len(candidates_list))
    if full_result["usage"] is not None:
        journal_token_usage("FULL", full_result["model"], full_result["section_estimates"], full_result["usage"])
    if shadow_result is not None and shadow_result["usage"] is not None:
        journal_token_usage("SHADOW", shadow_result["model"], shadow_result["section_estimates"], shadow_result["usage"])

    candidates = full_result["candidates"] or []
    risk_state = _risk_state(equity, positions, regime, breaker_status, "FULL", set(universe_data.keys()))
    # SELLs before BUYs, submitted market-on-open (OPG) for the next session
    # — never at decision-time prices.
    enriched = _validate_and_execute(candidates, risk_state, "FULL")

    final_positions = get_positions_with_sleeves()
    final_equity = get_equity()
    journal_brain_run(
        "FULL",
        regime,
        breaker_status,
        enriched,
        sweep_results,
        final_positions,
        final_equity,
        full_result.get("usage") or {},
        briefing_sections=briefing["sections"],
        v0_signals=v0_signals,
    )


# Rough chars-per-token heuristic (~4:1 for English prose), good enough to
# spot budget drift (§5) without pulling in a tokenizer just to print a debug view.
def _estimate_tokens(text):
    return round(len(text) / 4)


def _print_headline_line(headline):
    line = f"{headline['title']} | {headline['age_hours']}h | {headline['source']}"
    print(f"    {line}  (~{_estimate_tokens(line)} tok)")
    return _estimate_tokens(line)


def _print_news():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]

    print("=== Ticker headlines (last 24h, cap 5/ticker) ===")
    ticker_headlines = get_ticker_headlines(symbols, hours=24, cap=5)
    ticker_tokens = 0
    quiet_count = 0
    for symbol, headlines in ticker_headlines.items():
        if not headlines:
            quiet_count += 1
            continue
        print(f"  {symbol}:")
        for headline in headlines:
            ticker_tokens += _print_headline_line(headline)
    print(f"  ({quiet_count}/{len(symbols)} tickers with no headlines in window)")
    print(f"  subtotal: ~{ticker_tokens} tok (summaries excluded from this estimate — prompt-excluded by default)\n")

    print("=== Macro headlines (last 24h, cap 8) ===")
    macro_headlines = get_macro_headlines(hours=24, cap=8)
    macro_tokens = 0
    for headline in macro_headlines:
        macro_tokens += _print_headline_line(headline)
    print(f"  subtotal: ~{macro_tokens} tok\n")

    print(f"TOTAL headline tokens (prompt estimate): ~{ticker_tokens + macro_tokens} tok")


def _print_forecast(label, forecast):
    print(
        f"      {label}: {forecast['direction']:<5} conf={forecast['confidence']:.2f} — {forecast['rationale']}"
    )


def _print_brain_result(label, result):
    print(f"\n--- {label} ({result['model']}) ---")
    if result["fallback"]:
        print("  FALLBACK_HOLD — no usable output this run (see log for reason)")
        return
    for candidate in result["candidates"]:
        proposal = candidate["proposal"]
        print(f"  {candidate['symbol']}: {proposal['action']} {proposal['sleeve']} ${proposal['usd_amount']:.0f} conf={proposal['confidence']:.2f}")
        print(f"      thesis: {proposal['thesis']}")
        print(f"      falsifier: {proposal['falsifier']}")
        print(f"      risks: {', '.join(proposal['risks'])}")
        _print_forecast("1w", candidate["forecasts"]["h1w"])
        _print_forecast("1m", candidate["forecasts"]["h1m"])
        _print_forecast("3m", candidate["forecasts"]["h3m"])

    if result["usage"] is not None:
        usage = result["usage"]
        print(
            f"\n  tokens: input={usage['input_tokens']} output={usage['output_tokens']} "
            f"cache_read={usage['cache_read_input_tokens']} cache_write={usage['cache_creation_input_tokens']}"
        )
        for section, tokens in result["section_estimates"].items():
            print(f"    {section}: ~{tokens} tok")


def _dry_run():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]

    journal_entries = read_entries()
    equity = get_equity()
    breaker_status = check_circuit_breakers(journal_entries, equity)
    positions = get_positions_with_sleeves()

    bars = get_daily_bars(symbols, days=FULL_RUN_BAR_DAYS)
    universe_data = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    ticker_headlines = get_ticker_headlines(symbols, cap=HEADLINES_PER_TICKER_CAP)
    macro_headlines = get_macro_headlines(cap=HEADLINES_MACRO_CAP)

    candidates, scores = select_candidates(universe_data, positions, ticker_headlines)
    sleeve_utilization = _sleeve_utilization(positions, equity)

    briefing = build_briefing(
        candidates,
        universe_data,
        bars,
        positions,
        ticker_headlines,
        macro_headlines,
        regime,
        breaker_status,
        sleeve_utilization,
        equity,
    )

    print(f"Regime: {regime}  Breaker: {breaker_status}  Candidates: {len(candidates)}\n")
    print(briefing["text"])

    full_result, shadow_result = brain.run_full_analysis(briefing, len(candidates))
    _print_brain_result("FULL run", full_result)
    if full_result["usage"] is not None:
        journal_token_usage("FULL", full_result["model"], full_result["section_estimates"], full_result["usage"])

    if shadow_result is not None:
        _print_brain_result("SHADOW run (not executed)", shadow_result)
        if shadow_result["usage"] is not None:
            journal_token_usage(
                "SHADOW", shadow_result["model"], shadow_result["section_estimates"], shadow_result["usage"]
            )

    # risk.py verdicts are pure/no side effects, so showing them here doesn't
    # compromise --dry-run's no-execution contract — it just makes the
    # preview actually useful now that risk.py exists.
    if full_result["candidates"]:
        risk_state = _risk_state(equity, positions, regime, breaker_status, "FULL", set(universe_data.keys()))
        proposals = _flatten_proposals(full_result["candidates"])
        verdicts_by_symbol = {v["symbol"]: v for v in validate_proposals(proposals, risk_state)}
        preview = [{**c, "verdict": verdicts_by_symbol[c["symbol"]]} for c in full_result["candidates"]]
        _print_verdicts(preview)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--news",
        action="store_true",
        help="print ticker + macro headlines with token-count estimates, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run screener -> briefing -> brain (full) and print results + risk verdicts without executing, then exit",
    )
    parser.add_argument(
        "--mode",
        choices=["light", "full"],
        help="LIGHT (pre-open/midday: monitor/trim/de-risk only) or FULL (after-close: may open positions)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.news:
        _print_news()
        return

    if args.dry_run:
        _dry_run()
        return

    if args.mode is None:
        parser.error("--mode {light,full} is required for a live run")

    try:
        if args.mode == "light":
            _run_light()
        else:
            _run_full()
    except Exception as exc:
        journal_error(exc)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
