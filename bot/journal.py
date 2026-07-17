import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.config import DEFAULT_SLEEVE, ENV, RISK

LOG_DIR = Path("logs")
JOURNAL_PATH = LOG_DIR / "journal.jsonl"
RUN_HISTORY_PATH = LOG_DIR / "run_history.log"


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


def _positions_payload(positions):
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "avg_entry_price": float(p.avg_entry_price),
            "unrealized_pl": float(p.unrealized_pl),
            "sleeve": DEFAULT_SLEEVE,
        }
        for p in positions
    ]


def journal_run(regime, analyzed, actions, positions, equity, breaker_status):
    timestamp = datetime.now(timezone.utc).isoformat()
    positions_payload = _positions_payload(positions)

    record = {
        "timestamp": timestamp,
        "env": ENV,
        "regime": regime,
        "breaker_status": breaker_status,
        "indicators": analyzed,
        "actions": actions,
        "positions": positions_payload,
        "equity": equity,
    }
    _append(JOURNAL_PATH, json.dumps(record, default=str))

    submitted = sum(1 for a in actions if a["status"] == "SUBMITTED")
    rejected = sum(1 for a in actions if a["status"] == "REJECTED")
    skipped = sum(1 for a in actions if a["status"].startswith("SKIPPED_"))
    summary = (
        f"{timestamp} env={ENV} regime={regime} breaker={breaker_status} "
        f"symbols={len(analyzed)} submitted={submitted} rejected={rejected} skipped={skipped} "
        f"positions={len(positions_payload)} equity={equity:.2f}"
    )
    _append(RUN_HISTORY_PATH, summary)

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
