"""The override corpus: manual changes that undo what an automation just did.

A logbook row with a context_user_id is a person acting. When the same entity was
changed by an automation within the window before, to a different state, the
person's change is an override of that automation: a free label saying the
automation was wrong there. The corpus accumulates in /data/overrides.jsonl, one
JSON object per override, and the ranking is recomputed from it.

The ranking counts runs undone: overrides of one automation whose automation changes
fall within RUN_GAP_S of each other undid one run, whatever entities or devices that run
touched, so the rate compares like with like against the automation's fire count.
"""

from __future__ import annotations

import collections
import datetime as dt
import itertools
import json
import os
import urllib.parse

RUN_GAP_S = 5  # the changes of one automation run land within seconds of each other


def fetch_logbook(api, start: dt.datetime, end: dt.datetime) -> list[dict]:
    path = f"/logbook/{start.strftime('%Y-%m-%dT%H:%M:%S%z')}?end_time={urllib.parse.quote(end.strftime('%Y-%m-%dT%H:%M:%S%z'))}"
    return api.core(path)


def find_overrides(rows: list[dict], window_s: int) -> list[dict]:
    by_entity: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r.get("entity_id") and r.get("state") is not None and r.get("when"):
            by_entity[r["entity_id"]].append(r)
    found = []
    for entity, changes in by_entity.items():
        changes.sort(key=lambda r: r["when"])
        for i, r in enumerate(changes):
            if not r.get("context_user_id"):
                continue
            t = dt.datetime.fromisoformat(r["when"])
            for prev in reversed(changes[:i]):
                tp = dt.datetime.fromisoformat(prev["when"])
                if (t - tp).total_seconds() > window_s:
                    break
                if prev.get("context_domain") == "automation" and prev.get("state") != r.get("state"):
                    found.append(
                        {
                            "when": r["when"],
                            "entity_id": entity,
                            "automation": prev.get("context_entity_id") or "",
                            "automation_name": prev.get("context_entity_id_name") or prev.get("context_name") or "",
                            "automation_set": prev.get("state"),
                            "person_set": r.get("state"),
                            "seconds_later": round((t - tp).total_seconds(), 1),
                            "user": (r.get("context_user_id") or "")[:8],
                        }
                    )
                    break
    return found


def run_time(o: dict) -> dt.datetime:
    """When the automation made the change the person undid."""
    return dt.datetime.fromisoformat(o["when"]) - dt.timedelta(seconds=float(o.get("seconds_later") or 0))


def runs_undone(overrides: list[dict]) -> int:
    """Automation runs the overrides undid: one automation's changes chained within RUN_GAP_S are one run."""
    per: dict[str, list[dt.datetime]] = collections.defaultdict(list)
    for o in overrides:
        per[o.get("automation") or ""].append(run_time(o))
    n = 0
    for times in per.values():
        times.sort()
        n += 1 + sum(1 for a, b in itertools.pairwise(times) if (b - a).total_seconds() > RUN_GAP_S)
    return n


def automation_fires(rows: list[dict]) -> collections.Counter:
    fired: collections.Counter = collections.Counter()
    for r in rows:
        if r.get("domain") == "automation" or (r.get("entity_id") or "").startswith("automation."):
            fired[r.get("entity_id") or r.get("name") or "?"] += 1
    return fired


class Corpus:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "overrides.jsonl")
        self.fires_path = os.path.join(data_dir, "automation_fires.json")

    @staticmethod
    def _key(o: dict) -> tuple[str, str]:
        return (o.get("when") or "", o.get("entity_id") or "")

    def append(self, overrides: list[dict]) -> None:
        # Each run reads the last day, so a run between nightly ones meets overrides already stored.
        seen = {self._key(o) for o in self.rows()}
        new = []
        for o in overrides:
            if self._key(o) not in seen:
                seen.add(self._key(o))
                new.append(o)
        if not new:
            return
        with open(self.path, "a", encoding="utf-8") as fh:
            for o in new:
                fh.write(json.dumps(o) + "\n")

    def add_fires(self, day: str, fires: collections.Counter) -> None:
        doc = self.fires()
        doc[day] = dict(fires)
        for old in sorted(doc)[:-90]:  # ninety days of daily counts
            doc.pop(old, None)
        with open(self.fires_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def fires(self) -> dict:
        if not os.path.exists(self.fires_path):
            return {}
        with open(self.fires_path, encoding="utf-8") as fh:
            return json.load(fh)

    def rows(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out, seen = [], set()
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                o = json.loads(line)
                if self._key(o) not in seen:  # copies written before 0.4.0 count once
                    seen.add(self._key(o))
                    out.append(o)
        return out

    def ranking(self, days: int = 30, now: dt.datetime | None = None) -> list[dict]:
        cutoff = ((now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=days)).isoformat()
        recent = [o for o in self.rows() if o["when"] >= cutoff]
        fires_total: collections.Counter = collections.Counter()
        for day, counts in self.fires().items():
            if day >= cutoff[:10]:
                fires_total.update(counts)
        per: dict[str, dict] = {}
        for o in recent:
            a = per.setdefault(o["automation"], {"automation": o["automation"], "name": o["automation_name"], "overrides": 0, "rows": [], "entities": collections.Counter(), "last": o["when"]})
            a["overrides"] += 1
            a["rows"].append(o)
            a["entities"][o["entity_id"]] += 1
            a["last"] = max(a["last"], o["when"])
        out = []
        for a in per.values():
            fired = fires_total.get(a["automation"], 0)
            runs = runs_undone(a.pop("rows"))
            out.append({**a, "runs_undone": runs, "entities": dict(a["entities"].most_common(5)), "fired": fired, "rate": round(runs / fired, 3) if fired else None})
        out.sort(key=lambda x: (-x["runs_undone"], -x["overrides"]))
        return out
