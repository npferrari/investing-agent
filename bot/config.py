import os

from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("ENV", "paper")
assert ENV == "paper", "Live trading is not enabled yet; ENV must be 'paper'."

# Paper trading (active)
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER_ENDPOINT = "https://paper-api.alpaca.markets"

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
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLV", "XLE", "XLY", "XLP",
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
DEFAULT_SLEEVE = "TACTICAL"

# All percentages are plain numbers (10 means 10%), not fractions.
RISK = {
    # Position sizing, as % of current equity, by sleeve.
    "core_position_pct": 10,
    "tactical_position_pct": 6,
    "hedge_position_pct": 6,
    # Portfolio-level caps.
    "max_positions": 8,
    "max_ticker_pct": 15,
    "max_sector_pct": 30,
    "sleeve_core_max_pct": 60,
    "sleeve_tactical_max_pct": 35,
    "sleeve_hedge_max_pct": 10,
    "cash_floor_pct": 10,
    # Max % of equity invested, by account-age week (1-indexed, counted from
    # the first journal entry). Weeks not listed here (4+) are capped at
    # 100 - cash_floor_pct, the same steady-state ceiling as week 3.
    "deploy_ramp": {1: 40, 2: 70, 3: 90},
    # Per-sleeve stop-loss, as % unrealized P/L (negative).
    "stop_tactical_pct": -7,
    "stop_core_pct": -12,
    "stop_hedge_pct": -10,
    # Circuit breakers.
    "max_daily_loss_pct": 3.5,
    "max_weekly_loss_pct": 8,
    # Trade cadence.
    "max_trades_per_day": 6,
    "ticker_cooldown_days": 1,
    # Not yet enforced anywhere — no confidence score or cost-tracking
    # subsystem exists yet to check these against.
    "confidence_floor": 0.6,
    "daily_cost_cap_usd": 1.50,
}

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
