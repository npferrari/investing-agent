import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.config import ENV, RISK

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
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    positions_payload = _positions_payload(positions)
    actions = _actions_from_candidates(candidates)
    forecast_ledger = _forecast_ledger_rows(candidates, timestamp, run_mode)

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
    summary = (
        f"{timestamp} env={ENV} type={run_mode.lower()}_run regime={regime} breaker={breaker_status} "
        f"candidates={len(candidates)} approved={approved} rejected={rejected} submitted={submitted} "
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
