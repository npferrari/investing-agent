import json
from pathlib import Path

STATE_PATH = Path("logs") / "pending_fills.json"


def load():
    """[{"order_id", "symbol", "action", "sleeve", "usd_amount",
    "order_value", "decision_price", "decision_quote", "submitted_at"}, ...]
    for every SUBMITTED order execute_order() didn't see fill synchronously
    (the normal case for a DAY order queued off-hours for the next open) —
    the one place execute.reconcile_pending_fills() looks for orders whose
    real fill price still needs to be checked and journaled.
    """
    if not STATE_PATH.exists():
        return []
    with open(STATE_PATH) as f:
        return json.load(f)


def save(entries):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w") as f:
        # default=str: decision_quote.timestamp is a datetime, not natively
        # JSON-serializable (same handling journal.py uses for the journal).
        json.dump(entries, f, indent=2, sort_keys=True, default=str)


def add(entry):
    entries = load()
    entries.append(entry)
    save(entries)
