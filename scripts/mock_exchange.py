"""Local mock of the exchange and broker endpoints, for offline demos and
smoke tests.

Serves SYNTHETIC data in the formats the parsers expect, and adds a new
announcement on every BSE/NSE poll so the recorder has something to record.

    python scripts/mock_exchange.py --port 8765
    # in another terminal, point GATS at it (see scripts/smoke.env)

For a scripted run (``scripts/paper_smoke.py``) the module's knobs are set
from Python instead: ``CLOCK`` (whose time it is), ``AUTO_ROWS`` (off: only
scripted filings appear), ``SCRIPTED_NSE`` (filings that appear once the
clock passes their time) and ``ATTACHMENT_PDF`` (what an NSE attachment
contains). It also answers Upstox's candle URLs under ``/upstox``, in the
documented shape: newest first, the candle still forming included.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

IST = timezone(timedelta(hours=5, minutes=30))
_counter = itertools.count(1)
_bse_requests = itertools.count(1)
FLAKY_EVERY = 0  # set by --flaky: every Nth BSE API call answers {} like a throttled API
_bse_rows: list[dict[str, object]] = []
_nse_rows: list[dict[str, object]] = []

# --- knobs for scripted runs -----------------------------------------------------
CLOCK: Callable[[], datetime] = lambda: datetime.now(IST)  # noqa: E731
AUTO_ROWS = True  # a new filing on every poll
SCRIPTED_NSE: list[tuple[datetime, dict[str, object]]] = []  # (appears at, row)
ATTACHMENT_PDF = b"%PDF-1.4 mock attachment"
EOD_TURNOVER_LACS = 600.0  # Rs 6 crore a day: liquid enough for the risk limits
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)


def _now_ist() -> datetime:
    return CLOCK().astimezone(IST)


def _new_bse_row(port: int) -> dict[str, object]:
    n = next(_counter)
    stamp = _now_ist().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    return {
        "NEWSID": f"mock-{n}",
        "SCRIP_CD": 500000 + n,
        "SLONGNAME": f"Mock Company {n} Ltd",
        "CATEGORYNAME": "Company Update",
        "SUBCATNAME": "Award of Order / Receipt of Order",
        "NEWSSUB": f"Mock Company {n} - receipt of order",
        "HEADLINE": f"Order worth Rs {n * 10} crore",
        "ATTACHMENTNAME": f"mock-{n}.pdf",
        "NEWS_DT": stamp,
        "DissemDT": stamp,
        "News_submission_dt": stamp,
        "TotalPageCnt": 1,
    }


def _new_nse_row() -> dict[str, object]:
    n = next(_counter)
    stamp = _now_ist().strftime("%d-%b-%Y %H:%M:%S")
    return {
        "seq_id": str(900000 + n),
        "symbol": f"MOCK{n}",
        "desc": "Receipt of Order",
        "sm_name": f"Mock Company {n} Limited",
        "sm_isin": f"INE{n:06d}A01",
        "an_dt": stamp,
        "exchdisstime": stamp,
        "attchmntFile": "",
        "attchmntText": f"Order worth Rs {n * 10} crore",
    }


def nse_order_win(appears_at: datetime, port: int, symbol: str = "MOCK1") -> dict[str, object]:
    """An NSE order-win filing with an attachment, as the real API shows one."""
    stamp = appears_at.astimezone(IST).strftime("%d-%b-%Y %H:%M:%S")
    return {
        "seq_id": f"95{appears_at.astimezone(IST):%H%M%S}",
        "symbol": symbol,
        "desc": "Bagging/Receiving of orders/contracts",
        "sm_name": "Mock 1 Ltd",
        "sm_isin": "INE000001A01",
        "an_dt": stamp,
        "exchdisstime": stamp,
        "attchmntFile": f"http://127.0.0.1:{port}/nse-attach/{symbol}_order.pdf",
        "attchmntText": "Receipt of order",
    }


def _eod_csv(ddmmyyyy: str) -> bytes:
    day = datetime.strptime(ddmmyyyy, "%d%m%Y").strftime("%d-%b-%Y")
    header = (
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
        "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, "
        "DELIV_PER"
    )
    rows = [
        f"MOCK{i}, EQ, {day}, 100, 101, 105, 99, 104, 104.5, 102.2, {1000 * i}, "
        f"{EOD_TURNOVER_LACS}, {10 * i}, {400 * i}, 40.00"
        for i in range(1, 6)
    ]
    return ("\n".join([header, *rows]) + "\n").encode()


def candle_price(minute: datetime) -> float:
    """A slow, steady climb through the session (synthetic)."""
    local = minute.astimezone(IST)
    opened = datetime.combine(local.date(), SESSION_OPEN, IST)
    return 100 + 0.01 * ((local - opened).total_seconds() // 60)


def _candle(minute: datetime, forming: bool) -> list[object]:
    stamp = minute.astimezone(IST).isoformat()
    price = candle_price(minute)
    if forming:  # one trade so far: a half-built candle, as a live feed may show it
        return [stamp, price, price, price, price, 1, 0]
    return [stamp, price, price + 0.2, price - 0.2, price + 0.01, 10_000, 0]


def _candles(day: date, until: datetime | None) -> bytes:
    """One session's one-minute candles, newest first. With ``until`` (the
    intraday reply) only those begun by then, the last one still forming."""
    opened = datetime.combine(day, SESSION_OPEN, IST)
    closed = datetime.combine(day, SESSION_CLOSE, IST)
    end = closed if until is None else min(until.astimezone(IST), closed)
    candles, minute = [], opened
    while minute < end:
        forming = until is not None and minute + timedelta(minutes=1) > until.astimezone(IST)
        candles.append(_candle(minute, forming))
        minute += timedelta(minutes=1)
    return json.dumps({"status": "success", "data": {"candles": candles[::-1]}}).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "MockExchange/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _upstox(self, path: str) -> None:
        """``/upstox/v3/historical-candle/[intraday/]<key>/minutes/1[/<to>/<from>]``."""
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(401, b'{"status": "error"}', "application/json")
            return
        parts = [unquote(p) for p in path.split("/") if p]
        tail = parts[parts.index("historical-candle") + 1 :]
        now = _now_ist()
        if tail and tail[0] == "intraday":
            if now.weekday() < 5:
                body = _candles(now.date(), now)
            else:  # no session today: nothing to show
                body = b'{"status": "success", "data": {"candles": []}}'
            self._send(200, body, "application/json")
        elif len(tail) >= 5:
            self._send(200, _candles(date.fromisoformat(tail[3]), None), "application/json")
        else:
            self._send(404, b"unknown candle path", "text/plain")

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        port = self.server.server_address[1]
        if url.path == "/":
            self._send(200, b"<html>bse home</html>", "text/html")
        elif url.path == "/bse/api":
            query = parse_qs(url.query)
            if FLAKY_EVERY and next(_bse_requests) % FLAKY_EVERY == 0:
                self._send(200, b"{}", "application/json")
                return
            if query.get("strPrevDate") != query.get("strToDate"):
                # Like the real API: ranges are refused with an empty object.
                self._send(200, b"{}", "application/json")
                return
            if AUTO_ROWS:
                _bse_rows.insert(0, _new_bse_row(port))
            page = int(query.get("pageno", ["1"])[0])
            rows = _bse_rows[(page - 1) * 50 : page * 50]
            pages = max(1, (len(_bse_rows) + 49) // 50)
            for row in rows:
                row["TotalPageCnt"] = pages
            body = json.dumps({"Table": rows, "Table1": [{"ROWCNT": len(_bse_rows)}]})
            self._send(200, body.encode(), "application/json")
        elif url.path == "/nse/":
            self._send(200, b"<html>home</html>", "text/html")
        elif url.path == "/nse/api":
            if AUTO_ROWS:
                _nse_rows.insert(0, _new_nse_row())
            now = _now_ist()
            due = [row for appears_at, row in SCRIPTED_NSE if appears_at <= now]
            self._send(200, json.dumps([*reversed(due), *_nse_rows]).encode(), "application/json")
        elif url.path.startswith("/nse-attach/"):
            self._send(200, ATTACHMENT_PDF, "application/pdf")
        elif url.path.startswith("/upstox/"):
            self._upstox(url.path)
        elif url.path.startswith("/eod/sec_bhavdata_full_"):
            ddmmyyyy = url.path.rsplit("_", 1)[1].removesuffix(".csv")
            self._send(200, _eod_csv(ddmmyyyy), "text/csv")
        elif url.path == "/bands.csv":
            body = (
                b"Symbol,Series,Security Name,Band,Remarks\n"
                b"MOCK1,EQ,Mock 1,20,\nMOCK2,BE,Mock 2,5,\n"
            )
            self._send(200, body, "text/csv")
        elif url.path == "/instruments.csv":
            body = (
                b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, "
                b"ISIN NUMBER, FACE VALUE\nMOCK1,Mock 1 Ltd,EQ,06-OCT-2008,10,1,INE000001A01,10\n"
            )
            self._send(200, body, "text/csv")
        elif url.path.startswith("/attach-live/"):
            self._send(404, b"not here", "text/plain")  # exercise the fallback folder
        elif url.path.startswith("/attach-hist/"):
            self._send(200, b"%PDF-1.4 mock attachment", "application/pdf")
        else:
            self._send(404, b"unknown path", "text/plain")


def serve(port: int = 0) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 (port 0 = any free port); the caller runs
    ``serve_forever`` and shuts it down."""
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--flaky", type=int, default=0, help="every Nth BSE call returns {}")
    args = parser.parse_args()
    global FLAKY_EVERY
    FLAKY_EVERY = args.flaky
    server = serve(args.port)
    print(f"mock exchange on http://127.0.0.1:{args.port}")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
