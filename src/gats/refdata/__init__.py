"""Reference data: who is who, point-in-time.

Exchange identifiers (BSE scrip codes, NSE symbols, ISINs) change over time,
and several sources only publish today's file. This package turns daily
snapshots of such files into versioned tables, so research can ask "what did
this identifier mean on date D?" without looking ahead.
"""
