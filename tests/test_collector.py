"""Loop 4's action record: canonical actions, the stream, gaps that make a day incomplete (a stop of
the add-on included), and the baselines with their false positives."""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import collector as col

TZ = ZoneInfo("America/Chicago")


def st(state, at, user=None, parent=None, **attrs):
    return {"state": state, "attributes": attrs, "last_updated": at, "context": {"id": f"ctx-{at}", "user_id": user, "parent_id": parent}}


def ev(entity, old, new):
    return {"data": {"entity_id": entity, "old_state": old, "new_state": new}, "time_fired": new["last_updated"]}


def test_canonical_actions_keep_controllable_fields_only():
    assert col.canonical("light.a", st("off", "t"), st("on", "t", brightness=128)) == {"state": "on", "brightness": 128}
    assert col.canonical("light.a", st("on", "t", brightness=128), st("off", "t")) == {"state": "off"}
    assert col.canonical("light.a", st("on", "t", brightness=128, rgb_color=[1, 2, 3]), st("on", "t", brightness=128, rgb_color=[9, 9, 9])) == {}
    assert col.canonical("climate.c", st("cool", "t", temperature=72), st("cool", "t", temperature=70)) == {"temperature": 70}


def test_record_files_the_stream_and_user_actions_only(tmp_path):
    c = col.Collector(str(tmp_path), TZ)
    at = "2026-10-08T12:00:00+00:00"
    c.record(ev("light.a", st("off", at), st("on", at, user="u1", brightness=10)))
    c.record(ev("light.a", st("on", at), st("off", at, parent="p")))
    c.record(ev("sensor.power", st("1", at), st("2", at, user="u1")))
    c.record(ev("climate.c", st("cool", at), st("cool", at, hvac_action="cooling", current_temperature=75, temperature=72)))
    c.record(ev("light.b", st("on", at, brightness=5, effect="x"), st("on", at, user="u1", brightness=5, effect="y")))
    stream = [json.loads(x) for x in (tmp_path / "stream" / "2026-10-08.jsonl").read_text().splitlines()]
    actions = [json.loads(x) for x in (tmp_path / "actions" / "2026-10-08.jsonl").read_text().splitlines()]
    assert [r["entity_id"] for r in stream] == ["light.a", "light.a", "climate.c", "light.b"]
    assert stream[2]["hvac_action"] == "cooling" and stream[2]["current_temperature"] == 75
    assert [(a["entity_id"], a["action"], a["supported"]) for a in actions] == [("light.a", {"state": "on", "brightness": 10}, True), ("light.b", {}, False)]


def test_a_gap_or_a_stop_of_the_add_on_makes_a_day_incomplete(tmp_path):
    c = col.Collector(str(tmp_path), TZ)
    for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"):
        (tmp_path / "actions" / f"{day}.jsonl").write_text("")
    c.gap("2026-10-01T10:00:00+00:00", "2026-10-01T10:00:05+00:00", "add-on not running")
    c.gap("2026-10-03T15:00:00+00:00", "2026-10-03T16:00:00+00:00", "OSError")
    assert c.complete_days(dt.date(2026, 10, 5)) == ["2026-10-02", "2026-10-04"]
    assert c.complete_days(dt.date(2026, 10, 4)) == ["2026-10-02"], "today is never complete"


class Socket:
    def __init__(self, events, fail_after=True):
        self.events_, self.fail = events, fail_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def subscribe(self, kind):
        assert kind == "state_changed"
        return 7

    def events(self, sub):
        yield from self.events_
        if self.fail:
            raise OSError("connection reset")


def test_run_records_and_writes_the_gap_back_to_the_last_heartbeat(tmp_path):
    c = col.Collector(str(tmp_path), TZ)
    c.alive(dt.datetime(2026, 10, 8, 9, 0, tzinfo=dt.UTC))
    clock = iter(dt.datetime(2026, 10, 8, 12, 0, s, tzinfo=dt.UTC) for s in range(60))
    stop = threading.Event()
    at = "2026-10-08T12:00:00+00:00"
    calls = []

    def connect():
        calls.append(1)
        if len(calls) > 1:
            stop.set()
            raise OSError("down")
        return Socket([ev("light.a", st("off", at), st("on", at, user="u1"))])

    stop_wait = stop.wait
    stop.wait = lambda timeout=None: stop_wait(0)
    c.run(stop, connect, now=lambda: next(clock))
    gaps = c.gaps()
    assert gaps[0] == {"from": "2026-10-08T09:00:00+00:00", "to": "2026-10-08T12:00:00+00:00", "reason": "add-on not running"}
    assert (tmp_path / "actions" / "2026-10-08.jsonl").exists()


def test_the_score_names_its_false_positives_and_stays_provisional(tmp_path):
    c = col.Collector(str(tmp_path), TZ)
    c.gap("2026-09-30T00:00:00+00:00", "2026-09-30T00:00:01+00:00", "add-on not running")
    for i in range(9):
        day = dt.date(2026, 10, 1) + dt.timedelta(days=i)
        at = dt.datetime.combine(day, dt.time(7, 2), TZ).astimezone(dt.UTC).isoformat()
        row = {"at": at, "entity_id": "light.k", "context_id": f"c{i}", "user_id": "u", "action": {"state": "on"}, "before": {}, "supported": True}
        (tmp_path / "actions" / f"{day.isoformat()}.jsonl").write_text(json.dumps(row) + "\n")
    s = c.score(dt.date(2026, 10, 10))
    assert s["complete_days"] == 9 and s["provisional"] and s["train_days"] == 6 and s["test_days"] == 3
    assert s["none"] == {"reproduced": 0, "false_positives": 0}
    assert s["oracle_most_frequent"]["reproduced"] == 3
    # train Thu 10-01 to Tue 10-06: one weekday and one weekend 07:00 rule; test Wed to Fri, one action
    # each, predicted at the twelve boundaries of 07:00 to 07:55: 3 reproduced, 33 false positives
    assert s["hour_bucket"] == {"rules": 2, "reproduced": 3, "of": 3, "false_positives": 33}
