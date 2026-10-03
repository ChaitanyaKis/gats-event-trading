"""Strategies (T6.2): decide what to trade, never how or whether.

A strategy turns what it sees (events, bars, its own positions) into
signals. The engine models execution, the risk engine can veto anything,
and the same strategy object runs in backtest, paper and live: only the
runtime that feeds it changes.
"""
