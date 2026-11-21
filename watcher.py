#!/usr/bin/env python3
"""Personal watcher for the Karnataka High Court display board.

Run:   python3 watcher.py            (then open http://localhost:8765)

One background poll of the court's page, shared by all browser tabs (default every
45s, never faster than 30s). Your matters are stored in the browser only.
Also proxies the board's own 'passed over' and 'next cases' links, only when you click them.
Standard library only.
"""
import argparse, html, json, re, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOARD_URL = "https://judiciary.karnataka.gov.in/display_board_bench.php"
BENCHES = {"bengaluru": "Bengaluru", "dharwad": "Dharwad", "kalaburagi": "Kalaburagi"}
HERE = Path(__file__).parent

PANE_RE = re.compile(r"""id=["'](bengaluru|dharwad|kalaburagi)["']""")
ROW_RE = re.compile(r"""<tr class=["']display_row (\w+)["'][^>]*>(.*?)</tr>""", re.S)
NEXT_RE = re.compile(r"get_next_cases\((\d+),(\d+),(\d+),(\d+)")
BENCH_CODE = {"bengaluru": "B", "dharwad": "D", "kalaburagi": "K"}
BASE = "https://judiciary.karnataka.gov.in/"
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
UPDATED_RE = re.compile(r"Updated on:\s*(?:<[^>]+>\s*)*([0-9/]+\s+[0-9:]+\s*[AP]M)")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", s))).strip()


def norm_case(s):
    """'WP 34820/2026' / 'W.P. No. 34820 / 2026' -> 'WP 34820/2026'."""
    s = re.sub(r"[.\s]+", " ", s.upper()).replace(" NO ", " ").strip()
    m = re.match(r"([A-Z ()]+?)\s*(\d+)\s*/\s*(\d{4})", s)
    return f"{m.group(1).replace(' ', '')} {m.group(2)}/{m.group(3)}" if m else s


def parse_board(page):
    """Return {'updated': str|None, 'benches': {bench: [row,...]}}."""
    marks = [(m.start(), m.group(1)) for m in PANE_RE.finditer(page)]
    benches = {k: [] for k in BENCHES}
    for i, (start, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(page)
        for cls, body in ROW_RE.findall(page[start:end]):
            cells = [clean(c) for c in CELL_RE.findall(body)]
            if len(cells) < 5:
                continue
            ch, lst, sl, case, stage = cells[:5]
            status = {"sit": "sitting", "no_ses": "no_session", "no_sit": "no_sitting"}.get(cls, cls)
            row = {"hall": int(ch) if ch.isdigit() else ch, "status": status}
            if status == "sitting":
                row.update(list=lst, sl=int(sl) if sl.isdigit() else None,
                           case=case, case_norm=norm_case(case), stage=stage)
                m = NEXT_RE.search(html.unescape(body))
                if m:
                    row["sitcode"] = int(m.group(2))
            benches[name].append(row)
    m = UPDATED_RE.search(page)
    return {"updated": m.group(1) if m else None, "benches": benches}


def http(url, data=None):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "hck-watcher/0.2 (personal use by an advocate)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


_cache = {}


def cached(key, ttl, fn):
    """Tiny TTL cache so repeated clicks don't hit the court site."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def get_passed(bench, hall):
    """Passed-over cases for a hall (what the board's List No. link shows)."""
    txt = http(BASE + "passedover_cases.php", f"dataval={hall}~{bench}".encode())
    cases = [c.strip() for c in clean(txt).split(",") if c.strip()]
    return [c for c in cases if not c.lower().startswith("no passed")]


def get_next(bench, hall, sitcode, lst, sl):
    """Next few cases in a hall (what the board's Sl. No. link shows)."""
    q = urllib.parse.urlencode({"dataval": f"{hall}*{sitcode}*{lst}*{sl}*{bench}", "bench": bench})
    txt = http(BASE + "nextcases_displayboard.php?" + q)
    return [{"sl": int(a), "case": clean(b)}
            for a, b in re.findall(r"<tr><td>(\d+)</td><td[^>]*>(.*?)</td></tr>", txt, re.S)]


CL_SPLIT = re.compile(r"Cause List No\.\s*(\d+)")
CL_ROW = re.compile(
    r"""<td[^>]*>\s*((?:&nbsp;|\s)*\d+(?:\.\d+)*)\s*</td>\s*<td[^>]*id=["']casetd["'][^>]*>\s*([^<]+)""", re.S)


def parse_causelist(page):
    """{list_no: {sl: case_norm}} for one hall's cause list page (main items only, not 'connected with')."""
    parts = CL_SPLIT.split(page)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        items = out.setdefault(parts[i], {})
        for sl, case in CL_ROW.findall(parts[i + 1]):
            sl = html.unescape(sl).replace("\xa0", "").strip()
            if sl.isdigit():
                items.setdefault(sl, norm_case(html.unescape(case)))
    return out


def get_causelist(bench, hall):
    """Item number to case number for a hall (what the board's Court link opens)."""
    page = http(BASE + f"causelist_specific_c.php?bench={bench}&hallno={hall}")
    return parse_causelist(page)


class Poller:
    def __init__(self, interval):
        self.interval = max(30, interval)
        self.data, self.error, self.fetched_at = None, None, None
        self.lock = threading.Lock()

    def fetch(self):
        return http(BOARD_URL)

    def set_interval(self, secs):
        """Let the page choose the poll rate (clamped to 30-120s to stay polite)."""
        self.interval = min(120, max(30, int(secs)))

    def loop(self):
        while True:
            try:
                parsed = parse_board(self.fetch())
                with self.lock:
                    self.data, self.error, self.fetched_at = parsed, None, time.time()
            except Exception as e:  # keep last good data, surface the error
                msg = f"{type(e).__name__}: {e}"
                if "CERTIFICATE_VERIFY_FAILED" in msg:
                    msg += " (on a Mac, run 'Install Certificates.command' from your Python folder in Applications)"
                with self.lock:
                    self.error = msg
            started = time.time()
            while time.time() - started < self.interval:  # re-checked so a new setting applies
                time.sleep(1)

    def snapshot(self):
        with self.lock:
            return {"data": self.data, "error": self.error, "fetched_at": self.fetched_at,
                    "interval": self.interval, "server_time": time.time()}


def make_handler(poller):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
            if path == "/api/board":
                if q.get("interval", "").isdigit():
                    poller.set_interval(q["interval"])
                body, ctype = json.dumps(poller.snapshot()).encode(), "application/json"
            elif path in ("/api/passed", "/api/next", "/api/causelist"):
                try:
                    b = q.get("bench", "")
                    assert b in ("B", "D", "K")
                    h = int(q["hall"])
                    if path == "/api/causelist":
                        res = cached(("c", b, h), 900, lambda: get_causelist(b, h))
                        if not res:  # an empty page is not worth remembering
                            _cache.pop(("c", b, h), None)
                        out = {"ok": True, "lists": res}
                        body, ctype = json.dumps(out).encode(), "application/json"
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if path == "/api/passed":
                        res = cached(("p", b, h), 15, lambda: get_passed(b, h))
                    else:
                        sc, ls, sl = int(q.get("sitcode", 0)), int(q["list"]), int(q["sl"])
                        res = cached(("n", b, h, sc, ls, sl), 15, lambda: get_next(b, h, sc, ls, sl))
                    out = {"ok": True, "items": res}
                except Exception as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                body, ctype = json.dumps(out).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = (HERE / "index.html").read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=int, default=45, help="seconds between polls (min 30)")
    a = ap.parse_args()
    p = Poller(a.interval)
    threading.Thread(target=p.loop, daemon=True).start()
    print(f"Open http://localhost:{a.port}  (polling every {p.interval}s)")
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(p)).serve_forever()
