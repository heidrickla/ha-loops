"""Loop 4, induction over the house's history, first slice: the record a policy is later scored on.

A live subscription to `state_changed` keeps, per local day, every change of an actionable entity
(`stream/<date>.jsonl`, gzipped once the day is over) and every change a person made through Home
Assistant (`actions/<date>.jsonl`: the context carries a user). An action is canonical: the entity and
the controllable fields it changed (on/off, brightness, fan percentage, climate mode and targets, cover
position); a change of other attributes only stays in the stream. Connection gaps are written down,
and a day with a gap is not complete. The recorder keeps 30 days; this keeps every day.

The nightly score splits complete days in time order (the first two thirds develop, the last third is
held out) and scores trivial policies at five-minute boundaries: none (0 reproduced, 0 false
positives), the most frequent training action given the moment of an action (an oracle trigger), and
an hour bucket: a training action seen on two or more days in the same weekday-or-weekend hour is
predicted at every boundary of that hour. An action is reproduced when the policy predicted it at the
boundary before it; every other prediction is a false positive. It stays provisional until 60
complete days exist.
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import logging
import os
import threading
from zoneinfo import ZoneInfo

LOG = logging.getLogger("loops.collector")
DOMAINS = {
    "light": ("state", "brightness"),
    "fan": ("state", "percentage", "preset_mode"),
    "climate": ("state", "temperature", "target_temp_low", "target_temp_high", "fan_mode", "preset_mode"),
    "switch": ("state",),
    "input_boolean": ("state",),
    "lock": ("state",),
    "cover": ("state", "current_position"),
    "media_player": ("state",),
    "scene": ("state",),
    "vacuum": ("state",),
}
# kept in the stream for loop 7: what the thermostat was doing and what it read
CLIMATE_KEEP = ("hvac_action", "current_temperature")
BOUNDARY_S = 300
MIN_DAYS = 60
ALIVE_S = 60


def fields(entity_id: str, state: dict | None) -> dict:
    if not state:
        return {}
    keep = DOMAINS.get(entity_id.split(".", 1)[0], ())
    attrs = state.get("attributes") or {}
    out = {k: attrs.get(k) for k in keep if k != "state"}
    out["state"] = state.get("state")
    return out


def canonical(entity_id: str, old: dict | None, new: dict | None) -> dict:
    """The controllable fields that changed, with their new values; empty when none did."""
    before, after = fields(entity_id, old), fields(entity_id, new)
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    if after.get("state") == "off":
        # off carries no brightness or speed to choose
        return {"state": "off"} if "state" in changed else {}
    return changed


def label(action: dict) -> str:
    return action["entity_id"] + " " + json.dumps(action["action"], sort_keys=True)


class Collector:
    def __init__(self, data_dir: str, tz: ZoneInfo):
        self.dir = data_dir
        self.tz = tz
        for sub in ("stream", "actions"):
            os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
        self.lock = threading.Lock()
        self.connected_since: str | None = None

    def _day(self, at: dt.datetime) -> str:
        return at.astimezone(self.tz).date().isoformat()

    def _append(self, sub: str, day: str, doc: dict) -> None:
        with self.lock, open(os.path.join(self.dir, sub, f"{day}.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, separators=(",", ":")) + "\n")

    def gap(self, start: str, end: str, reason: str) -> None:
        with self.lock, open(os.path.join(self.dir, "gaps.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"from": start, "to": end, "reason": reason}) + "\n")

    def record(self, event: dict) -> None:
        data = event.get("data") or {}
        entity_id = data.get("entity_id") or ""
        domain = entity_id.split(".", 1)[0]
        if domain not in DOMAINS:
            return
        old, new = data.get("old_state"), data.get("new_state")
        if not new:
            return
        at = dt.datetime.fromisoformat(new.get("last_updated") or event.get("time_fired"))
        ctx = new.get("context") or {}
        attrs = new.get("attributes") or {}
        row = {"at": at.isoformat(), "entity_id": entity_id, **fields(entity_id, new), "context_id": ctx.get("id"), "user": bool(ctx.get("user_id")), "parent": bool(ctx.get("parent_id"))}
        if domain == "climate":
            row.update({k: attrs.get(k) for k in CLIMATE_KEEP})
        day = self._day(at)
        self._append("stream", day, row)
        if ctx.get("user_id"):
            action = canonical(entity_id, old, new)
            self._append(
                "actions",
                day,
                {
                    "at": at.isoformat(),
                    "entity_id": entity_id,
                    "context_id": ctx.get("id"),
                    "user_id": ctx.get("user_id"),
                    "action": action,
                    "before": fields(entity_id, old),
                    "supported": bool(action),
                },
            )

    def alive(self, at: dt.datetime) -> None:
        tmp = os.path.join(self.dir, "alive.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": at.isoformat(timespec="seconds")}, fh)
        os.replace(tmp, os.path.join(self.dir, "alive.json"))

    def last_alive(self) -> str | None:
        path = os.path.join(self.dir, "alive.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("at")

    def run(self, stop: threading.Event, connect, now=lambda: dt.datetime.now(dt.UTC)) -> None:
        """Subscribe and record until stopped; reconnect with backoff, writing each gap. The gap of a
        restart runs from the last heartbeat before it, so time the add-on was not running counts."""
        backoff, reason = 5, "add-on not running"
        down_since = self.last_alive() or now().isoformat(timespec="seconds")
        while not stop.is_set():
            try:
                with connect() as ws:
                    sub = ws.subscribe("state_changed")
                    up = now()
                    self.gap(down_since, up.isoformat(timespec="seconds"), reason)
                    self.connected_since, backoff, beat = up.isoformat(timespec="seconds"), 5, up
                    self.alive(up)
                    for event in ws.events(sub):
                        self.record(event)
                        t = now()
                        if (t - beat).total_seconds() >= ALIVE_S:
                            self.alive(t)
                            beat = t
                        if stop.is_set():
                            break
            except Exception as err:  # the gap is the record of it
                reason = f"{type(err).__name__}: {err}"[:200]
                LOG.warning("collector disconnected: %s", reason)
            down_since, self.connected_since = self.last_alive() or now().isoformat(timespec="seconds"), None
            if stop.wait(backoff):
                break
            backoff = min(backoff * 2, 300)

    def compress(self, today: str) -> int:
        """Gzip each finished day's stream file."""
        n = 0
        folder = os.path.join(self.dir, "stream")
        for name in sorted(os.listdir(folder)):
            if name.endswith(".jsonl") and name[:10] < today:
                path = os.path.join(folder, name)
                with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                    dst.write(src.read())
                os.remove(path)
                n += 1
        return n

    def gaps(self) -> list[dict]:
        path = os.path.join(self.dir, "gaps.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def complete_days(self, today: dt.date) -> list[str]:
        """Days before today the subscription covered from local midnight to midnight."""
        actions = sorted(n[:10] for n in os.listdir(os.path.join(self.dir, "actions")) if n.endswith(".jsonl"))
        streams = sorted(n[:10] for n in os.listdir(os.path.join(self.dir, "stream")) if n.endswith((".jsonl", ".jsonl.gz")))
        seen = sorted(set(actions) | set(streams))
        if not seen:
            return []
        gaps = self.gaps()
        first_up = min((g["to"] for g in gaps), default=None)
        out = []
        for day in seen:
            d = dt.date.fromisoformat(day)
            if d >= today:
                continue
            start = dt.datetime.combine(d, dt.time(0), self.tz)
            end = start + dt.timedelta(days=1)
            if first_up is None or dt.datetime.fromisoformat(first_up) > start:
                continue
            if any(dt.datetime.fromisoformat(g["from"]) < end and dt.datetime.fromisoformat(g["to"]) > start for g in gaps):
                continue
            out.append(day)
        return out

    def today(self, day: str) -> dict:
        """Rows filed so far today: a connected collector with no stream rows is not recording."""
        out = {"date": day}
        for sub in ("stream", "actions"):
            path = os.path.join(self.dir, sub, f"{day}.jsonl")
            out[sub] = 0
            if os.path.exists(path):
                with self.lock, open(path, encoding="utf-8") as fh:
                    out[sub] = sum(1 for _ in fh)
        return out

    def actions(self, day: str) -> list[dict]:
        path = os.path.join(self.dir, "actions", f"{day}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def score(self, today: dt.date) -> dict:
        days = self.complete_days(today)
        rows = {d: self.actions(d) for d in days}
        total = sum(len(v) for v in rows.values())
        supported = {d: [a for a in v if a["supported"]] for d, v in rows.items()}
        out: dict = {
            "complete_days": len(days),
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
            "actions": total,
            "supported": sum(len(v) for v in supported.values()),
            "contexts": len({a["context_id"] for v in supported.values() for a in v}),
            "provisional": len(days) < MIN_DAYS,
            "needed_days": MIN_DAYS,
        }
        if len(days) < 3:
            out["reason"] = f"{len(days)} complete days; a split needs 3"
            return out
        cut = (len(days) * 2) // 3
        train, test = days[:cut], days[cut:]
        test_actions = [a for d in test for a in supported[d]]
        out.update(train_days=len(train), test_days=len(test), test_actions=len(test_actions))
        counts = collections.Counter(label(a) for d in train for a in supported[d])
        out["none"] = {"reproduced": 0, "false_positives": 0}
        if counts:
            top, _ = counts.most_common(1)[0]
            hit = sum(1 for a in test_actions if label(a) == top)
            out["oracle_most_frequent"] = {"label": top, "reproduced": hit, "of": len(test_actions)}
        buckets: dict[tuple[bool, int], set[str]] = collections.defaultdict(set)
        seen_days: dict[tuple[bool, int, str], set[str]] = collections.defaultdict(set)
        for d in train:
            for a in supported[d]:
                local = dt.datetime.fromisoformat(a["at"]).astimezone(self.tz)
                seen_days[(local.weekday() >= 5, local.hour, label(a))].add(d)
        for (weekend, hour, lab), ds in seen_days.items():
            if len(ds) >= 2:
                buckets[(weekend, hour)].add(lab)
        reproduced = predicted = 0
        for d in test:
            start = dt.datetime.combine(dt.date.fromisoformat(d), dt.time(0), self.tz)
            hits: set[tuple[int, str]] = set()
            for a in supported[d]:
                local = dt.datetime.fromisoformat(a["at"]).astimezone(self.tz)
                boundary = int((local - start).total_seconds()) // BOUNDARY_S
                labs = buckets.get((local.weekday() >= 5, local.hour), set())
                if label(a) in labs:
                    hits.add((boundary, label(a)))
            reproduced += len(hits)
            for slot in range(24 * 3600 // BOUNDARY_S):
                at = start + dt.timedelta(seconds=slot * BOUNDARY_S)
                predicted += len(buckets.get((at.weekday() >= 5, at.hour), set()))
        out["hour_bucket"] = {"rules": sum(len(v) for v in buckets.values()), "reproduced": reproduced, "of": len(test_actions), "false_positives": predicted - reproduced}
        return out
