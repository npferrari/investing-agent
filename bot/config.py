import os

from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("ENV", "paper")
assert ENV == "paper", "Live trading is not enabled yet; ENV must be 'paper'."

# Paper trading (active)
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER_ENDPOINT = "https://paper-api.alpaca.markets"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Human-tunable only — the Strategy Brain may never rewrite which model runs
# a given cadence slot (PRD §7: "Neither brain may modify its own or the
# other's prompts or code").
BRAIN_MODEL_FULL = "claude-sonnet-5"
BRAIN_MODEL_LIGHT = "claude-haiku-4-5"
# Set to a model string to also query a second model on the identical
# briefing during full runs (shadow proposals are journaled/forecast-ledgered
# but never executed). None disables shadow mode.
SHADOW_MODEL = None

# Live trading (stubs, unused while ENV == "paper")
ALPACA_LIVE_API_KEY = os.getenv("ALPACA_LIVE_API_KEY")
ALPACA_LIVE_SECRET_KEY = os.getenv("ALPACA_LIVE_SECRET_KEY")
ALPACA_LIVE_ENDPOINT = "https://api.alpaca.markets"

STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "NFLX", "AMZN", "TSLA",
    "HD", "KO", "PG", "COST",
    "JPM", "V", "BAC",
    "UNH", "JNJ", "LLY",
    "XOM", "CVX", "COP",
    "CAT", "HON", "UNP",
    "NEE", "DUK",
    "AMT",
    "LIN", "APD", "FCX",
]

ETFS = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLC", "XLI", "XLU", "XLB", "XLRE",
    "TLT", "IEF", "GLD", "VEA", "EEM",
]

HEDGE = ["SH"]

UNIVERSE = {
    "STOCKS": STOCKS,
    "ETFS": ETFS,
    "HEDGE": HEDGE,
}

# Every position is currently attributed to this sleeve — execute_order
# doesn't yet support issuing BUYs under any other sleeve, and there is no
# persisted per-position sleeve store, so a mixed-sleeve book can't yet exist.
# (Renamed CORE/TACTICAL/HEDGE -> MOMENTUM/QUALITY_VALUE/DEFENSIVE per the
# guide's step-11 sleeve rename; positions_state.json sleeve tagging itself
# is step 13's job, not this one's.)
# Kept equal to main.py's v0 SLEEVE tag (MOMENTUM, per the 2026-07-24 build
# review's re-tag) — execute.stop_loss_sweep() looks up a position's stop
# threshold via this constant with no per-position sleeve to consult, so if
# this drifts from whatever v0 is actually tagging its BUYs, every held
# position gets checked against the wrong sleeve's stop-loss (-10 vs -12)
# until step 13's real sleeve tracking replaces this entirely.
DEFAULT_SLEEVE = "MOMENTUM"

# All percentages are plain numbers (10 means 10%), not fractions.
# NOTE — this rename is names-only for the sizing/cap values below (still the
# old CORE/TACTICAL/HEDGE numbers under new keys); aligning them with the
# PRD/guide's actual M40/QV35/D25 baselines and bounds is separate follow-up
# work that belongs with risk.py (next step), not this rename pass.
# stop_*_pct is the one exception: those three values are corrected to match
# G7 exactly (MOMENTUM -10 / QUALITY_VALUE -12 / DEFENSIVE -10) rather than
# carried over renamed-but-wrong, since a stop-loss threshold silently off by
# 2-5pp is a safety-relevant bug, not a cosmetic rename detail.
RISK = {
    # Position sizing, as % of current equity, by sleeve.
    "momentum_position_pct": 10,
    "quality_value_position_pct": 6,
    "defensive_position_pct": 6,
    # Portfolio-level caps.
    "max_positions": 8,
    "max_ticker_pct": 15,
    "max_sector_pct": 30,
    "sleeve_momentum_max_pct": 60,
    "sleeve_quality_value_max_pct": 35,
    "sleeve_defensive_max_pct": 10,
    "cash_floor_pct": 10,
    # Max % of equity invested, by account-age week (1-indexed, counted from
    # the first journal entry). Weeks not listed here (4+) are capped at
    # 100 - cash_floor_pct, the same steady-state ceiling as week 3.
    "deploy_ramp": {1: 40, 2: 70, 3: 90},
    # Per-sleeve stop-loss, as % unrealized P/L (negative) — matches G7 exactly.
    "stop_momentum_pct": -10,
    "stop_quality_value_pct": -12,
    "stop_defensive_pct": -10,
    # Circuit breakers.
    "max_daily_loss_pct": 3.5,
    "max_weekly_loss_pct": 8,
    # Trade cadence.
    "max_trades_per_day": 6,
    "ticker_cooldown_days": 1,
    # G9: proposals below this confidence are rejected by bot/risk.py.
    "confidence_floor": 0.6,
    "daily_cost_cap_usd": 1.50,
}

# Slippage haircut for full-run market-on-open fills, applied analytically in
# the journal (reported P&L is net of this, not simulated in the order
# itself — OPG fills at whatever the actual open print is). PRD §8 open item
# 3 asks for separate US/EU parameters; the universe is US-only today, so one
# value suffices until international tickers are added.
SLIPPAGE_HAIRCUT_PCT = 0.05

# Stop-loss sweep confirmation (research/v1-architecture.md §5, 2026-07-23
# decision): a breach must hold on a second quote fetched this many seconds
# later before selling, filtering a single bad print rather than trusting one
# reading. A confirmed stop still implying a single-day move past this
# threshold is anomalous enough to flag (STOP_ANOMALY) and page a human.
STOP_CONFIRM_DELAY_SECONDS = 30
STOP_ANOMALY_SINGLE_DAY_PCT = 15

# Screener/briefing budgets (T2, T6).
MAX_CANDIDATES_NEW = 12  # discovery slots; held positions ride along uncapped
HEADLINES_PER_TICKER_CAP = 5
HEADLINES_MACRO_CAP = 8
NEWS_SPIKE_SUMMARY_CAP = 2  # max candidates whose full summary reaches the prompt
# T6's guide estimate was ~100 tokens/candidate; a live probe against the
# actual schema (3 forecasts with rationale + thesis/falsifier/risks) showed
# a single candidate alone needs ~500-550 output tokens even with thinking
# disabled — the schema is simply richer than that estimate assumed. Kept
# here, not silently raised without comment, since it changes the real
# per-run output-token cost from the guide's stated ~1.2k budget to roughly
# 5x that at full candidate counts (still cheap in absolute dollars: Sonnet
# 5 output pricing puts a 12-candidate run around $0.06-0.09, comfortably
# inside the $1.50/day cap, but worth knowing before trusting the digest's
# cost-per-run assumption).
OUTPUT_TOKENS_PER_CANDIDATE = 550

SECTOR_MAP = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "GOOGL": "Communication Services",
    "META": "Communication Services",
    "NFLX": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "KO": "Consumer Staples",
    "PG": "Consumer Staples",
    "COST": "Consumer Staples",
    "JPM": "Financials",
    "V": "Financials",
    "BAC": "Financials",
    "UNH": "Health Care",
    "JNJ": "Health Care",
    "LLY": "Health Care",
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "CAT": "Industrials",
    "HON": "Industrials",
    "UNP": "Industrials",
    "NEE": "Utilities",
    "DUK": "Utilities",
    "AMT": "Real Estate",
    "LIN": "Materials",
    "APD": "Materials",
    "FCX": "Materials",
}

# Sector -> sector-rotation ETF, for briefing.py's neighborhood block. Every
# SECTOR_MAP sector now has a dedicated ETF (2026-07-24 build review added
# XLC/XLI/XLU/XLB/XLRE, closing the SPY-fallback gap the first pass flagged)
# — briefing._neighborhood's SPY fallback path is now reachable only for
# non-sector ETFs (broad/bond/gold/international/hedge), which have no
# "3 stocks from that sector" concept regardless of ETF coverage.
SECTOR_ETF_MAP = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}

# Assets DEFENSIVE may hold — mirrors the brain.py system prompt's sleeve
# description (kept in sync manually since the persona text is a fixed,
# human-authored string per PRD §7, not derived from this list). Used by
# risk.py once it exists (G4/G2); XLU joined 2026-07-24 alongside the sector
# ETF buildout.
DEFENSIVE_ELIGIBLE_ASSETS = ["TLT", "IEF", "GLD", "XLP", "XLU", "SH"]
