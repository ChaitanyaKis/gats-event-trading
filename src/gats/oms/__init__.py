"""Order management for the live pilot (M8): the gate, the caps, the orders.

Three independent locks stand between this code and a real order:

1. ``GATS_LIVE_ENABLED`` is false by default.
2. ``gats live`` refuses without a human's approval record for exactly this
   design and these caps, made at an interactive terminal after gate G3.
3. Every order passes the hard caps; a breach switches trading off.

None of them has a bypass, and none may ever get one.
"""
