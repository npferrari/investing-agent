import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

from bot.config import ENV
from bot.execute import DEFAULT_SLEEVE, get_equity, get_positions

LOG_DIR = Path("logs")
JOURNAL_PATH = LOG_DIR / "journal.jsonl"
RUN_HISTORY_PATH = LOG_DIR / "run_history.log"


def _append(path, line):
    LOG_DIR.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(line + "\n")


def _positions_payload():
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "avg_entry_price": float(p.avg_entry_price),
            "unrealized_pl": float(p.unrealized_pl),
            "sleeve": DEFAULT_SLEEVE,
        }
        for p in get_positions()
    ]


def journal_run(regime, analyzed, actions):
    timestamp = datetime.now(timezone.utc).isoformat()
    positions = _positions_payload()
    equity = get_equity()

    record = {
        "timestamp": timestamp,
        "env": ENV,
        "regime": regime,
        "indicators": analyzed,
        "actions": actions,
        "positions": positions,
        "equity": equity,
    }
    _append(JOURNAL_PATH, json.dumps(record, default=str))

    submitted = sum(1 for a in actions if a["status"] == "SUBMITTED")
    rejected = sum(1 for a in actions if a["status"] == "REJECTED")
    skipped = sum(1 for a in actions if a["status"] == "SKIPPED_ALREADY_HELD")
    summary = (
        f"{timestamp} env={ENV} regime={regime} symbols={len(analyzed)} "
        f"buys_submitted={submitted} buys_rejected={rejected} buys_skipped_held={skipped} "
        f"positions={len(positions)} equity={equity:.2f}"
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
