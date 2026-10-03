"""Market data beyond the daily files (M5): one-minute bars from the broker.

``bars`` stores them as Parquet and reads them with DuckDB; ``upstox``
fetches them, raw first, month by month and resumably.
"""
