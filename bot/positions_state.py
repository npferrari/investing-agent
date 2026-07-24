import json
from pathlib import Path

from bot.config import DEFAULT_SLEEVE

STATE_PATH = Path("logs") / "positions_state.json"


def load():
    """{symbol: {"sleeve": str, "opened_at": iso-timestamp}} for every
    position this codebase has opened since this file started tracking.
    Positions that predate this file (or were opened before step 13) simply
    have no entry here — callers fall back to DEFAULT_SLEEVE for those,
    same as the rest of the codebase did before this module existed.
    """
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def save(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def record_open(symbol, sleeve, opened_at):
    state = load()
    state[symbol] = {"sleeve": sleeve, "opened_at": opened_at}
    save(state)


def record_close(symbol):
    state = load()
    state.pop(symbol, None)
    save(state)


def get_sleeve(symbol, state=None):
    state = state if state is not None else load()
    return state.get(symbol, {}).get("sleeve", DEFAULT_SLEEVE)
