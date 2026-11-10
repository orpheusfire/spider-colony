#!/usr/bin/env python3
"""Spider Colony web app. Run: python3 server.py  ->  http://127.0.0.1:8765  (local only)."""
import json
import re
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import colony

WEB = Path(__file__).resolve().parent / "web"
PORT = 8765
PROFILE_KEYS = {"name", "phone", "email", "sms_on", "email_on", "include_united_be", "require_unlimited_mileage", "hide_budget"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        db = colony.connect()
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/scout":
            return self._send(200, colony.scout_status(db))
        if path == "/api/state":
            ws = [colony.watch_summary(db, w) for w in db.execute("SELECT * FROM watches WHERE status!='done' ORDER BY id DESC")]
            return self._send(200, {"watches": ws, "profile": colony.profile(db), "max_steps": colony.MAX_LOOSEN_STEPS})
        m = re.fullmatch(r"/api/search/(\d+)", path)
        if m:
            rid = int(m.group(1))
            req = db.execute("SELECT * FROM search_requests WHERE id=?", (rid,)).fetchone()
            res = [dict(r) for r in db.execute("SELECT * FROM search_results WHERE request_id=? ORDER BY price", (rid,))]
            target_total = colony.search_target_total(req["category"], json.loads(req["params"])) if req else None
            params = json.loads(req["params"]) if req else {}
            hide = colony.profile(db).get("hide_budget") == "1"
            for r in res:
                r["budget"] = bool(req and req["category"] == "flight" and colony.is_budget(r["title"]))
                if req and req["category"] == "car" and r["price"] is not None:
                    if r["url"] and r["url"] not in colony.VENDOR_HOME.values():
                        r["prefilled"] = "/reservation/" in r["url"]  # a specific link was stored with the result: keep it
                    else:
                        url, pre = colony.vendor_link(r["title"].split("·")[0], params)
                        if url:
                            r["url"], r["prefilled"] = url, pre
                if r["price"] is not None:
                    if r.get("prefilled"):
                        r["link_kind"] = "prefilled"
                    elif not r["url"]:
                        r["link_kind"] = None
                    else:  # a plain company home page vs a specific booking page
                        path = urlparse(r["url"]).path
                        r["link_kind"] = "home" if path in ("", "/") else "page"
                r["within_target"] = None if (target_total is None or r["price"] is None) else r["price"] <= target_total
            res = [r for r in res if not (hide and r["budget"])]
            return self._send(200, {"status": req["status"] if req else "missing", "results": res, "target_total": target_total,
                                    "category": req["category"] if req else None, "params": params})
        m = re.fullmatch(r"/api/watch/(\d+)/history", path)
        if m:
            rows = [dict(r) for r in db.execute("SELECT ts, source, price, currency, basis, is_deal FROM prices WHERE watch_id=? ORDER BY ts DESC LIMIT 200", (int(m.group(1)),))]
            return self._send(200, rows)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        db = colony.connect()
        path = self.path.split("?")[0]
        body = self._body()
        if path in ("/api/watch", "/api/search"):
            problem = colony.check_dates(body.get("category"), body.get("params", {}))
            if problem:
                return self._send(400, {"error": problem})
        if path == "/api/watch":
            dup = colony.find_duplicate_watch(db, body["category"], body.get("params", {}), body.get("target"), body.get("target_unit", "total"))
            if dup:
                return self._send(200, {"id": dup, "duplicate": True})
            wid = colony.add_watch(db, body["category"], body["label"], body.get("params", {}), body.get("target"), body.get("target_unit", "total"))
            return self._send(200, {"id": wid})
        if path == "/api/search":
            cur = db.execute("INSERT INTO search_requests (category, params, created) VALUES (?,?,?)",
                             (body["category"], json.dumps(body.get("params", {})), colony.now().isoformat()))
            db.commit()
            return self._send(200, {"id": cur.lastrowid})
        if path == "/api/scout/extend":
            return self._send(200, colony.scout_extend(db))
        if path == "/api/profile":
            for k, v in body.items():
                if k in PROFILE_KEYS:
                    db.execute("INSERT OR REPLACE INTO profile (k, v) VALUES (?, ?)", (k, str(v)))
            db.commit()
            return self._send(200, colony.profile(db))
        m = re.fullmatch(r"/api/watch/(\d+)/(loosen|tighten|pause|resume|delete|target)", path)
        if m:
            wid, act = int(m.group(1)), m.group(2)
            w = db.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
            if not w:
                return self._send(404, {"error": "no such watch"})
            if act == "loosen":
                db.execute("UPDATE watches SET loosen_steps=MIN(loosen_steps+1, ?) WHERE id=?", (colony.MAX_LOOSEN_STEPS, wid))
            elif act == "tighten":
                db.execute("UPDATE watches SET loosen_steps=MAX(loosen_steps-1, 0) WHERE id=?", (wid,))
            elif act == "pause":
                db.execute("UPDATE watches SET status='paused' WHERE id=?", (wid,))
            elif act == "resume":
                db.execute("UPDATE watches SET status='active' WHERE id=?", (wid,))
            elif act == "delete":
                db.execute("UPDATE watches SET status='done' WHERE id=?", (wid,))
            elif act == "target":
                db.execute("UPDATE watches SET target=?, loosen_steps=0 WHERE id=?", (body.get("target"), wid))
            db.commit()
            return self._send(200, colony.watch_summary(db, db.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()))
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    colony.connect()
    print(f"Spider Colony running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
