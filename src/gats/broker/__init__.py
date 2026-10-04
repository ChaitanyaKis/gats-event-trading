"""Brokers (M8): the only code that can send a real order.

Everything here is off unless a human has switched live trading on
(``GATS_LIVE_ENABLED``) *and* created an approval record with
``gats gate approve``. Claude never does either.
"""
