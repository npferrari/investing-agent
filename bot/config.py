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
