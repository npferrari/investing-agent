from dotenv import load_dotenv; load_dotenv()
import os

from alpaca.trading.client import TradingClient
tc = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
acct = tc.get_account()
print("✅ Alpaca:", acct.status, "| buying power:", acct.buying_power)

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
dc = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
trade = dc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols="AAPL"))
print("✅ Market data: AAPL last trade $", trade["AAPL"].price)

import anthropic
msg = anthropic.Anthropic().messages.create(
    model="claude-sonnet-5", max_tokens=20,
    messages=[{"role": "user", "content": "Reply with exactly: agent brain online"}])
print("✅ Claude API:", msg.content[0].text)
