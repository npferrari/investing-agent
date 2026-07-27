import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.config import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, ENV, PRICING_PER_MTOK, RISK

LOG_DIR = Path("logs")
JOURNAL_PATH = LOG_DIR / "journal.jsonl"
RUN_HISTORY_PATH = LOG_DIR / "run_history.log"

# Forecast horizon -> days until expiry, for the forecast ledger (Layer 4's
# "important new artifact" — see research/v1-architecture.md's audit entry).
FORECAST_HORIZON_DAYS = (("h1w", 7), ("h1m", 30), ("h3m", 90))


def _append(path, line):
    LOG_DIR.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(line + "\n")


def read_entries():
    if not JOURNAL_PATH.exists():
        return []
    entries = []
    with open(JOURNAL_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def get_ramp_cap_pct(entries):
    dated = [e for e in entries if "equity" in e and "timestamp" in e]
    if not dated:
        week = 1
    else:
        first_ts = min(datetime.fromisoformat(e["timestamp"]) for e in dated)
        age_days = (datetime.now(timezone.utc) - first_ts).days
        week = age_days // 7 + 1

    ramp = RISK["deploy_ramp"]
    if week in ramp:
        return ramp[week]
    return 100 - RISK["cash_floor_pct"]


def count_trades_today(entries, action="BUY"):
    today = datetime.now(timezone.utc).date()
    count = 0
    for e in entries:
        ts = e.get("timestamp")
        if not ts or datetime.fromisoformat(ts).date() != today:
            continue
        for a in e.get("actions", []):
            if a.get("status") == "SUBMITTED" and a.get("action") == action:
                count += 1
    return count


def symbols_in_cooldown(entries, cooldown_days, action="BUY"):
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    cooling = set()
    for e in entries:
        ts = e.get("timestamp")
        if not ts or datetime.fromisoformat(ts) < cutoff:
            continue
        for a in e.get("actions", []):
            if a.get("status") == "SUBMITTED" and a.get("action") == action:
                cooling.add(a["symbol"])
    return cooling


def position_opened_date(entries, symbol):
    """First timestamp of the current unbroken streak of holding `symbol`,
    inferred from the journal's own historical position snapshots.

    This is the SECONDARY source for "days held" — positions_state.json's
    own `opened_at` (recorded precisely at BUY time) is authoritative when
    present; briefing._position_line() only falls back to this journal-scan
    inference for a position positions_state.reconcile() reports as
    untracked (a manual trade, a pre-tracking position, or a stale dust
    remainder). Walks runs newest-first, keeps going back while the symbol
    is still held, and returns the timestamp of the oldest run in that
    unbroken streak. Returns None if the symbol isn't currently held, has
    no journal history, or the journal has a gap in that streak (e.g. the
    2026-07-24 git-add bug that silently dropped several runs' commits).
    """
    dated = sorted(
        (e for e in entries if "timestamp" in e and "positions" in e),
        key=lambda e: e["timestamp"],
        reverse=True,
    )
    opened_at = None
    for e in dated:
        held = any(p["symbol"] == symbol and float(p.get("qty", 0)) != 0 for p in e["positions"])
        if not held:
            break
        opened_at = e["timestamp"]
    return opened_at


def _positions_payload(positions):
    """`positions` are already-resolved dicts from
    execute.get_positions_with_sleeves() — sleeve/opened_at came from
    positions_state.reconcile() upstream, once, not re-derived here."""
    return [
        {
            "symbol": p["symbol"],
            "qty": p["qty"],
            "market_value": p["market_value"],
            "avg_entry_price": p["avg_entry_price"],
            "unrealized_pl": p["unrealized_pl"],
            "sleeve": p["sleeve"],
        }
        for p in positions
    ]


def count_risk_adding_trades(entries, days=7):
    """G8: BUYs into MOMENTUM or QUALITY_VALUE in the trailing N days — BUYs
    into DEFENSIVE are risk-reducing (PRD §4) and excluded, matching
    bot.risk._is_risk_adding's definition exactly."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    for e in entries:
        ts = e.get("timestamp")
        if not ts or datetime.fromisoformat(ts) < cutoff:
            continue
        for a in e.get("actions", []):
            if a.get("status") == "SUBMITTED" and a.get("action") == "BUY" and a.get("sleeve") != "DEFENSIVE":
                count += 1
    return count


def in_construction_window(entries):
    """First 5 (calendar) days since the first journal entry — the same
    calendar-day proxy for "trading days" that get_ramp_cap_pct already
    uses for its week-counting, applied here for G8's construction-window
    exemption."""
    dated = [e for e in entries if "equity" in e and "timestamp" in e]
    if not dated:
        return True
    first_ts = min(datetime.fromisoformat(e["timestamp"]) for e in dated)
    age_days = (datetime.now(timezone.utc) - first_ts).days
    return age_days < 5


def _forecast_ledger_rows(candidates, timestamp, run_mode):
    created_at = datetime.fromisoformat(timestamp)
    rows = []
    for c in candidates:
        for horizon, days in FORECAST_HORIZON_DAYS:
            forecast = c["forecasts"][horizon]
            rows.append(
                {
                    "symbol": c["symbol"],
                    "horizon": horizon,
                    "direction": forecast["direction"],
                    "confidence": forecast["confidence"],
                    "rationale": forecast["rationale"],
                    "sleeve": c["proposal"]["sleeve"],
                    "created_at": timestamp,
                    "expires_at": (created_at + timedelta(days=days)).isoformat(),
                    "resolved": False,
                    "run_mode": run_mode,
                }
            )
    return rows


def _actions_from_candidates(candidates):
    """Flatten approved+executed candidates into execute_order()'s own
    result shape, so count_trades_today/symbols_in_cooldown/
    count_risk_adding_trades keep working unchanged against these newer
    run types."""
    return [c["execution"] for c in candidates if c.get("execution") is not None]


def journal_brain_run(
    run_mode,
    regime,
    breaker_status,
    candidates,
    sweep_results,
    positions,
    equity,
    token_usage,
    briefing_sections=None,
    v0_signals=None,
    fill_results=None,
    fallback_events=None,
    token_budget_warnings=None,
):
    """The full paper trail for one LIGHT or FULL run: every forecast (with
    its expiry, for scorecard.py to grade once it exists), every proposal
    with its thesis/falsifier/risks, its risk.py verdict, and its execution
    (if any) — this is what closes the Layer-4 gap flagged in the 2026-07-28
    brain audit (research/v1-architecture.md), where `--dry-run` only ever
    journaled token usage.

    `candidates`: list of {"symbol", "forecasts", "proposal", "verdict",
    "execution"} — verdict is risk.py's result dict, execution is
    execute_order()'s result dict or None if never submitted (rejected, or
    a HOLD with nothing to execute).

    `briefing_sections` / `v0_signals` are FULL-run-only (None for LIGHT):
    a light run never screens the discovery universe or computes v0's
    benchmark signal, so there's nothing to log for either.

    `fill_results` is execute.reconcile_pending_fills()'s return value for
    *this* run — orders a previous run submitted but couldn't confirm filled
    at the time, resolved now (filled, with real slippage vs. the
    decision-time price; or a terminal failure that never filled at all).
    Unrelated to this run's own candidates/actions, which is why it's kept
    as its own top-level field rather than folded into `actions`.

    `fallback_events` / `token_budget_warnings` come straight from
    brain.run_full_analysis / run_light_analysis's own return dicts — one
    entry per symbol (or "ALL" for a FULL run's single batch call) whose
    brain call ended in FALLBACK_HOLD, or whose input tokens exceeded the
    §5 budget (bot.config.TOKEN_BUDGET_INPUT).
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    positions_payload = _positions_payload(positions)
    actions = _actions_from_candidates(candidates)
    forecast_ledger = _forecast_ledger_rows(candidates, timestamp, run_mode)
    fill_results = fill_results or []
    fallback_events = fallback_events or []
    token_budget_warnings = token_budget_warnings or []

    record = {
        "timestamp": timestamp,
        "env": ENV,
        "type": f"{run_mode.lower()}_run",
        "regime": regime,
        "breaker_status": breaker_status,
        "sweep_results": sweep_results,
        "candidates": candidates,
        "forecast_ledger": forecast_ledger,
        "actions": actions,
        "fill_results": fill_results,
        "fallback_events": fallback_events,
        "token_budget_warnings": token_budget_warnings,
        "positions": positions_payload,
        "equity": equity,
        "token_usage": token_usage,
    }
    if briefing_sections is not None:
        record["briefing_sections"] = briefing_sections
    if v0_signals is not None:
        record["v0_signals"] = v0_signals

    _append(JOURNAL_PATH, json.dumps(record, default=str))

    approved = sum(1 for c in candidates if c["verdict"]["status"] == "APPROVED")
    rejected = sum(1 for c in candidates if c["verdict"]["status"] == "REJECTED")
    submitted = sum(1 for a in actions if a["status"] == "SUBMITTED")
    # Distinct from `rejected` above (a risk.py verdict rejection, before
    # execution is ever attempted): this counts an *approved* order that
    # then bounced at the broker — e.g. the 2026-07-25 OPG/fractional bug,
    # where approved=9 rejected=3 submitted=0 gave no way to tell "nothing
    # was approved" apart from "something was approved and silently failed
    # to place" without reading the full journal record.
    execution_rejected = sum(1 for a in actions if a["status"] == "REJECTED")
    filled = sum(1 for f in fill_results if f["status"] == "FILLED")
    fill_failed = len(fill_results) - filled
    summary = (
        f"{timestamp} env={ENV} type={run_mode.lower()}_run regime={regime} breaker={breaker_status} "
        f"candidates={len(candidates)} approved={approved} rejected={rejected} submitted={submitted} "
        f"execution_rejected={execution_rejected} fills_reconciled={filled} fills_failed={fill_failed} "
        f"fallback_hold={len(fallback_events)} token_budget_warn={len(token_budget_warnings)} "
        f"sweep_events={len(sweep_results)} positions={len(positions_payload)} equity={equity:.2f}"
    )
    _append(RUN_HISTORY_PATH, summary)

    return record


def journal_token_usage(run_mode, model, section_estimates, usage):
    """Record one brain.py API call's token spend, by section, for the cost cap (G11/§5).

    A distinct record type (no "equity"/"actions" keys) so it's invisible to
    the equity- and trade-history readers above (get_ramp_cap_pct,
    count_trades_today, symbols_in_cooldown all key off fields this record
    doesn't have). Logged for dry runs too — cost visibility shouldn't depend
    on whether the run was allowed to execute.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    record = {
        "timestamp": timestamp,
        "env": ENV,
        "type": "token_usage",
        "run_mode": run_mode,
        "model": model,
        "section_estimates": section_estimates,
        "usage": usage,
    }
    _append(JOURNAL_PATH, json.dumps(record, default=str))
    _append(
        RUN_HISTORY_PATH,
        f"{timestamp} env={ENV} type=token_usage run_mode={run_mode} model={model} "
        f"input={usage.get('input_tokens')} output={usage.get('output_tokens')} "
        f"cache_read={usage.get('cache_read_input_tokens')} cache_write={usage.get('cache_creation_input_tokens')}",
    )
    return record


def _model_cost_usd(model, usage):
    pricing = PRICING_PER_MTOK.get(model)
    if pricing is None or not usage:
        return 0.0
    return (
        usage.get("input_tokens", 0) * pricing["input"]
        + usage.get("output_tokens", 0) * pricing["output"]
        + usage.get("cache_creation_input_tokens", 0) * pricing["input"] * CACHE_WRITE_MULTIPLIER
        + usage.get("cache_read_input_tokens", 0) * pricing["input"] * CACHE_READ_MULTIPLIER
    ) / 1_000_000


def today_token_cost_usd(entries):
    """Sum of every token_usage record's estimated cost (bot.config's
    PRICING_PER_MTOK) journaled so far today (UTC). main.py's G11/§5 cost-cap
    check reads this *before* calling the brain at all, so a capped day
    skips the API call entirely rather than paying for it and rejecting the
    result afterward."""
    today = datetime.now(timezone.utc).date()
    total = 0.0
    for e in entries:
        if e.get("type") != "token_usage":
            continue
        ts = e.get("timestamp")
        if not ts or datetime.fromisoformat(ts).date() != today:
            continue
        total += _model_cost_usd(e.get("model"), e.get("usage") or {})
    return total


def journal_cost_cap_skip(run_mode, breaker_status, sweep_results, positions, equity, fill_results, today_cost, cap):
    """A distinct record type for the G11/§5 cost-cap trip: the brain was
    never called this run (today's journaled spend already exceeds
    daily_cost_cap_usd), only the unconditional stop-loss sweep and pending-
    fill reconciliation ran. No `candidates`/`actions` here — there's
    nothing to report from a call that never happened."""
    timestamp = datetime.now(timezone.utc).isoformat()
    positions_payload = _positions_payload(positions)
    record = {
        "timestamp": timestamp,
        "env": ENV,
        "type": "cost_cap_skip",
        "run_mode": run_mode,
        "breaker_status": breaker_status,
        "sweep_results": sweep_results,
        "fill_results": fill_results or [],
        "positions": positions_payload,
        "equity": equity,
        "today_cost_usd": today_cost,
        "daily_cost_cap_usd": cap,
    }
    _append(JOURNAL_PATH, json.dumps(record, default=str))
    _append(
        RUN_HISTORY_PATH,
        f"{timestamp} env={ENV} type=cost_cap_skip run_mode={run_mode} breaker={breaker_status} "
        f"today_cost={today_cost:.4f} cap={cap:.2f} sweep_events={len(sweep_results)} "
        f"positions={len(positions_payload)} equity={equity:.2f}",
    )
    return record


def journal_error(exc):
    timestamp = datetime.now(timezone.utc).isoformat()
    record = {
        "timestamp": timestamp,
        "env": ENV,
        "status": "ERROR",
        "error": str(exc),
        "traceback": traceback.format_exc(),
    }
    _append(JOURNAL_PATH, json.dumps(record, default=str))
    _append(RUN_HISTORY_PATH, f"{timestamp} env={ENV} status=ERROR error={exc!r}")
    return record
