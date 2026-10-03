"""Running a strategy forward in time (M7): paper trading.

The paper runtime is the backtest engine fed live: filings from the
recorder, one-minute bars from the broker's market-data API. Nothing here
can place an order with a broker.
"""
