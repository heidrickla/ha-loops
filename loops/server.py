"""Loops: the recurring improvement loops, hosted inside Home Assistant.

    python3 server.py --options /data/options.json --data /data --port 8095

Once a day at `run_at` (local time) the check suite runs against the Supervisor and
core APIs with the add-on's own token, the last day of the logbook is mined for
overrides, the HVAC energy per degree-day is brought up to date (loop 7) and the
manual-action record is scored (loop 4), findings and the corpus are written under
/data, and a summary goes to the notify target. A websocket subscription records
manual actions as they happen. The ingress page shows the latest findings, the
override ranking, the HVAC weeks, the action record and the run history;
/api/latest.json, /api/overrides.json, /api/controller.json and /api/actions.json
serve the same to other tools. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checks
import collector as col
import controller as ctl
import hass_ws
import overrides as ov
import page
from zoneinfo import ZoneInfo

LOG = logging.getLogger("loops")


def load_options(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc if isinstance(doc, dict) else {}


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(os.path.join(data_dir, "findings"), exist_ok=True)

    def _read(self, name: str, default):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, name: str, doc) -> None:
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)

    def state(self) -> dict:
        return self._read("state.json", {})

    def save_state(self, state: dict) -> None:
        self._write("state.json", state)

    def latest(self) -> dict | None:
        return self._read("latest.json", None)

    def history(self) -> list[dict]:
        return self._read("history.json", [])

    def save_run(self, run: dict) -> None:
        self._write(os.path.join("findings", f"{run['ran_at'][:10]}.json"), run)
        self._write("latest.json", run)
        history = self.history()
        history.append({"ran_at": run["ran_at"], "counts": run["counts"], "overrides": run["overrides"], "seconds": run["seconds"]})
        self._write("history.json", history[-365:])


class Loops:
    def __init__(self, options: dict, data_dir: str, api: checks.Api | None = None, now=None):
        self.options = options
        self.store = Store(data_dir)
        self.corpus = ov.Corpus(data_dir)
        self.api = api or checks.Api(os.environ.get("SUPERVISOR_TOKEN", ""))
        self.now = now or (lambda: dt.datetime.now(dt.UTC))
        self.lock = threading.Lock()
        self.controller = ctl.Controller(data_dir)
        self.data_dir = data_dir
        self._tz: ZoneInfo | None = None
        self._collector: col.Collector | None = None
        self.socket = lambda: hass_ws.CoreSocket(os.environ.get("SUPERVISOR_TOKEN", ""))

    def tz(self) -> ZoneInfo:
        """Home Assistant's own time zone, read once; local days are its days."""
        if self._tz is None:
            self._tz = ZoneInfo(self.api.core("/config")["time_zone"])
        return self._tz

    @property
    def collector(self) -> col.Collector:
        if self._collector is None:
            self._collector = col.Collector(self.data_dir, self.tz())
        return self._collector

    def loop7(self, now: dt.datetime) -> list[dict]:
        try:
            today = now.astimezone(self.tz()).date()
            with self.socket() as ws:
                s = self.controller.update(ws, self.options, self.tz(), today)
        except ctl.ConfigError as err:
            return [checks.finding("hvac_per_degree_day", "info", f"loop 7 not measuring: {err}")]
        except Exception as err:
            return [checks.finding("hvac_per_degree_day", "warn", f"HVAC statistics read failed: {type(err).__name__}: {err}")]
        out = []
        day = s["latest_day"]
        if day and (day["temperature"]["missing"] or not all(z["complete"] for z in day["zones"].values())):
            gaps = [f"temperature {day['temperature']['missing']}h"] if day["temperature"]["missing"] else []
            gaps += [f"{n} {sum(c['missing'] for c in z['circuits'].values())}h" for n, z in day["zones"].items() if not z["complete"]]
            out.append(checks.finding("hvac_per_degree_day", "warn", f"HVAC inputs missing on {day['date']}: {', '.join(gaps)}", value=day))
        # the newest week always, unavailable reasons included, and the newest measured one when older
        weeks = [s["latest_week"]] if s["latest_week"] else []
        if s["latest_measured_week"] and (not weeks or s["latest_measured_week"]["week"] != weeks[0]["week"]):
            weeks.append(s["latest_measured_week"])
        for week in weeks:
            parts = [f"{n} {z['kwh_per_cdd']} ({z['days_counted']}d)" if z["kwh_per_cdd"] is not None else f"{n} unavailable ({z['reason']})" for n, z in week["zones"].items()]
            out.append(checks.finding("hvac_per_degree_day", "info", f"kWh per CDD65, {week['week']} (arm {', '.join(week['arms'])}): {'; '.join(parts)}", value=week))
        return out

    def loop4(self, now: dt.datetime) -> list[dict]:
        try:
            today = now.astimezone(self.tz()).date()
            self.collector.compress(today.isoformat())
            s = self.collector.score(today)
        except Exception as err:
            return [checks.finding("manual_actions", "warn", f"action record unreadable: {type(err).__name__}: {err}")]
        title = f"manual actions: {s['complete_days']} complete days of {s['needed_days']}, {s['supported']} actions"
        if "hour_bucket" in s:
            hb = s["hour_bucket"]
            title += f"; hour buckets reproduce {hb['reproduced']} of {hb['of']} held out with {hb['false_positives']} false positives"
        if s["provisional"]:
            title += " (provisional)"
        return [checks.finding("manual_actions", "info", title, value=s)]

    def run_once(self) -> dict:
        with self.lock:
            started = time.monotonic()
            now = self.now()
            state = self.store.state()
            findings, new_state = checks.run_all(self.api, self.options, state, now)
            found: list[dict] = []
            fires = {}
            try:
                rows = ov.fetch_logbook(self.api, now - dt.timedelta(days=1), now)
                found = ov.find_overrides(rows, int(self.options.get("override_window_s") or 300))
                fires = ov.automation_fires(rows)
                self.corpus.append(found)
                self.corpus.add_fires(now.date().isoformat(), fires)
            except Exception as err:
                findings.append(checks.finding("overrides", "warn", f"logbook read failed: {type(err).__name__}: {err}"))
            findings += self.loop7(now)
            findings += self.loop4(now)
            counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("action", "warn", "info")}
            run = {
                "ran_at": now.isoformat(timespec="seconds"),
                "findings": findings,
                "counts": counts,
                "overrides": len(found),
                "automations_fired": sum(fires.values()) if fires else 0,
                "seconds": round(time.monotonic() - started, 1),
            }
            new_state["last_run"] = run["ran_at"]
            self.store.save_state(new_state)
            self.store.save_run(run)
            self.notify(run)
            LOG.info("run: %s findings %s, %d overrides, %.1fs", counts, "", len(found), run["seconds"])
            return run

    def notify(self, run: dict) -> None:
        target = (self.options.get("notify_target") or "").strip()
        if not target:
            return
        c = run["counts"]
        top = sorted(run["findings"], key=lambda f: {"action": 0, "warn": 1, "info": 2}.get(f["severity"], 3))
        lines = [f"{c['action']} action, {c['warn']} warn, {c['info']} info; {run['overrides']} overrides yesterday"]
        lines += [f"- {f['title']}" for f in top[:4]]
        # The memory review has its own line, so a busy night does not push it out of the top four.
        lines += [f"- {f['title']}" for f in top[4:] if f["check"] == "memory_review"]
        code = self.api.post_core(f"/services/notify/{target}", {"title": "Loops: nightly checks", "message": "\n".join(lines)})
        if code >= 300:
            LOG.warning("notify %s answered %s", target, code)

    def seconds_until_next_run(self, local_now: dt.datetime) -> float:
        hh, mm = (self.options.get("run_at") or "03:30").split(":")
        target = local_now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= local_now:
            target += dt.timedelta(days=1)
        return (target - local_now).total_seconds()

    def scheduler(self, stop: threading.Event) -> None:
        state = self.store.state()
        today = dt.datetime.now().date().isoformat()
        if not (state.get("last_run") or "").startswith(today):
            with contextlib.suppress(Exception):
                self.run_once()
        while not stop.is_set():
            wait = self.seconds_until_next_run(dt.datetime.now())
            if stop.wait(wait):
                break
            try:
                self.run_once()
            except Exception:
                LOG.exception("nightly run failed")


def make_handler(loops: Loops):
    class Handler(BaseHTTPRequestHandler):
        server_version = "loops/0.1"

        def log_message(self, fmt, *args):
            LOG.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
            elif path == "/api/latest.json":
                self._send(200, json.dumps(loops.store.latest()).encode(), "application/json")
            elif path == "/api/overrides.json":
                self._send(200, json.dumps(loops.corpus.ranking()).encode(), "application/json")
            elif path == "/api/snapshot.json":
                try:
                    doc = checks.snapshot(loops.api)
                except Exception as err:
                    # A snapshot that could not be read must not look like an empty one.
                    self._send(502, json.dumps({"error": type(err).__name__}).encode(), "application/json")
                    return
                self._send(200, json.dumps(doc).encode(), "application/json")
            elif path == "/api/controller.json":
                self._send(200, json.dumps({"days": loops.controller.days()[-60:], "weeks": loops.controller.weeks()}).encode(), "application/json")
            elif path == "/api/actions.json":
                c = loops.collector
                score = next((f["value"] for f in (loops.store.latest() or {}).get("findings", []) if f["check"] == "manual_actions"), None)
                today = loops.now().astimezone(loops.tz()).date().isoformat()
                doc = {"connected_since": c.connected_since, "today": c.today(today), "gaps": c.gaps()[-50:], "score": score}
                self._send(200, json.dumps(doc).encode(), "application/json")
            elif path == "/api/history.json":
                self._send(200, json.dumps(loops.store.history()).encode(), "application/json")
            elif path in ("/", "/index.html"):
                body = page.render(loops.store.latest(), loops.corpus.ranking(), loops.store.history(), loops.controller.weeks()).encode()
                self._send(200, body, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/api/run":
                run = loops.run_once()
                self._send(200, json.dumps({"ran_at": run["ran_at"], "counts": run["counts"], "overrides": run["overrides"]}).encode(), "application/json")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

    return Handler


def serve(loops: Loops, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(loops))
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--options", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=8095)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    loops = Loops(load_options(args.options), args.data)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    try:
        ping = loops.api.get("/supervisor/ping")
    except Exception as err:  # the first thing to know when every check reads 401
        ping = f"{type(err).__name__}: {err}"
    LOG.info("supervisor token present: %s (%d chars); ping: %s", bool(token), len(token), ping)
    server = serve(loops, args.port)
    stop = threading.Event()
    threading.Thread(target=loops.scheduler, args=(stop,), name="scheduler", daemon=True).start()
    try:
        threading.Thread(target=loops.collector.run, args=(stop, loops.socket), name="collector", daemon=True).start()
    except Exception:  # without Home Assistant's time zone there is no local day to file by
        LOG.exception("collector not started")
    LOG.info("serving on :%d, nightly run at %s", args.port, loops.options.get("run_at"))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
