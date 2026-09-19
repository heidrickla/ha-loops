"""The override corpus: manual changes that undo what an automation just did.

A logbook row with a context_user_id is a person acting. When the same entity was
changed by an automation within the window before, to a different state, the
person's change is an override of that automation: a free label saying the
automation was wrong there. The corpus accumulates in /data/overrides.jsonl, one
JSON object per override, and the ranking is recomputed from it.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import urllib.parse


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

    def append(self, overrides: list[dict]) -> None:
        if not overrides:
            return
        with open(self.path, "a", encoding="utf-8") as fh:
            for o in overrides:
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
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def ranking(self, days: int = 30) -> list[dict]:
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
        recent = [o for o in self.rows() if o["when"] >= cutoff]
        fires_total: collections.Counter = collections.Counter()
        for day, counts in self.fires().items():
            if day >= cutoff[:10]:
                fires_total.update(counts)
        per: dict[str, dict] = {}
        for o in recent:
            a = per.setdefault(o["automation"], {"automation": o["automation"], "name": o["automation_name"], "overrides": 0, "entities": collections.Counter(), "last": o["when"]})
            a["overrides"] += 1
            a["entities"][o["entity_id"]] += 1
            a["last"] = max(a["last"], o["when"])
        out = []
        for a in sorted(per.values(), key=lambda x: -x["overrides"]):
            fired = fires_total.get(a["automation"], 0)
            out.append({**a, "entities": dict(a["entities"].most_common(5)), "fired": fired, "rate": round(a["overrides"] / fired, 3) if fired else None})
        return out
