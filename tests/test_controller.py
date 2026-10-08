"""Loop 7's HVAC energy per degree-day: local days across clock changes, unavailable never zero, a
week's ratio of sums over counting days, arms, and only incomplete days fetched again."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import controller as ctl

TZ = ZoneInfo("America/Chicago")
ZONES = {"central": ["sensor.ac_c", "sensor.fu_c"], "bedrooms": ["sensor.ac_b", "sensor.fu_b"]}
TEMP = "sensor.outdoor"


def hours_for(day: dt.date, temp: float = 85.0, kwh: float = 0.5, skip: dict | None = None) -> dict[str, dict[int, float]]:
    skip = skip or {}
    out: dict[str, dict[int, float]] = {}
    for sid in [*ZONES["central"], *ZONES["bedrooms"], TEMP]:
        value = temp if sid == TEMP else kwh
        out[sid] = {h: value for i, h in enumerate(ctl.expected_hours(day, TZ)) if i not in skip.get(sid, ())}
    return out


def test_a_local_day_has_23_or_25_hours_across_a_clock_change():
    assert len(ctl.expected_hours(dt.date(2026, 3, 8), TZ)) == 23
    assert len(ctl.expected_hours(dt.date(2026, 11, 1), TZ)) == 25
    assert len(ctl.expected_hours(dt.date(2026, 7, 1), TZ)) == 24


def test_a_complete_cooling_day_counts_and_a_missing_hour_is_unavailable_not_zero():
    day = dt.date(2026, 7, 1)
    row = ctl.day_row(day, TZ, ZONES, TEMP, hours_for(day), "baseline")
    assert row["cdd65"] == pytest.approx(20.0) and row["hdd65"] == 0 and row["eligible"]
    assert row["zones"]["central"]["kwh"] == pytest.approx(24.0) and row["zones"]["central"]["complete"]
    gap = ctl.day_row(day, TZ, ZONES, TEMP, hours_for(day, skip={"sensor.fu_b": {5}}), "baseline")
    assert gap["zones"]["bedrooms"] == {**gap["zones"]["bedrooms"], "complete": False, "kwh": None}
    assert gap["zones"]["bedrooms"]["circuits"]["sensor.fu_b"]["missing"] == 1
    assert gap["zones"]["central"]["complete"], "a gap in one zone leaves the other whole"
    cold = ctl.day_row(day, TZ, ZONES, TEMP, hours_for(day, skip={TEMP: {0}}), "baseline")
    assert cold["cdd65"] is None and cold["eligible"] is None and "missing 1 of 24" in cold["reason"]
    mild = ctl.day_row(day, TZ, ZONES, TEMP, hours_for(day, temp=66.0), "baseline")
    assert mild["eligible"] is False and "shoulder" in mild["reason"]


def week(days: int, start: dt.date = dt.date(2026, 7, 6), **kw) -> list[dict]:
    return [ctl.day_row(start + dt.timedelta(days=i), TZ, ZONES, TEMP, hours_for(start + dt.timedelta(days=i), **kw), "baseline") for i in range(days)]


def test_a_week_is_a_ratio_of_sums_over_five_or_more_counting_days():
    rows = week(7)
    w = ctl.week_rows(rows, list(ZONES))
    assert len(w) == 1 and w[0]["week"] == "2026-W28" and w[0]["arms"] == ["baseline"]
    assert w[0]["zones"]["central"] == {"days_counted": 7, "kwh": 168.0, "cdd65": 140.0, "kwh_per_cdd": pytest.approx(1.2), "reason": ""}
    assert w[0]["zones"]["house"]["kwh"] == 336.0
    short = ctl.week_rows(rows[:4], list(ZONES))[0]["zones"]["central"]
    assert short["kwh_per_cdd"] is None and short["reason"] == "4 counting days, 5 needed"
    rows[0] = ctl.day_row(dt.date(2026, 7, 6), TZ, ZONES, TEMP, hours_for(dt.date(2026, 7, 6), skip={"sensor.ac_b": {1}}), "baseline")
    rows[1] = ctl.day_row(dt.date(2026, 7, 7), TZ, ZONES, TEMP, hours_for(dt.date(2026, 7, 7), skip={"sensor.ac_b": {1}}), "baseline")
    rows[2] = ctl.day_row(dt.date(2026, 7, 8), TZ, ZONES, TEMP, hours_for(dt.date(2026, 7, 8), skip={"sensor.ac_b": {1}}), "baseline")
    w = ctl.week_rows(rows, list(ZONES))[0]["zones"]
    assert w["central"]["days_counted"] == 7 and w["bedrooms"]["kwh_per_cdd"] is None and w["house"]["days_counted"] == 4


class FakeWS:
    def __init__(self, days: list[dt.date]):
        self.calls: list[dict] = []
        self.days = days

    def call(self, kind, **kw):
        self.calls.append(kw)
        out = {}
        for sid in kw["statistic_ids"]:
            rows = []
            for day in self.days:
                for h, v in hours_for(day)[sid].items():
                    key = "mean" if sid == TEMP else "change"
                    rows.append({"start": h * 1000, key: v})
            out[sid] = rows
        return out


def test_update_fetches_only_incomplete_days_and_keeps_each_day_s_arm(tmp_path):
    options = {"hvac_zones": ["central=sensor.ac_c,sensor.fu_c", "bedrooms=sensor.ac_b,sensor.fu_b"], "outdoor_temperature": TEMP, "controller_from": "2026-07-06"}
    c = ctl.Controller(str(tmp_path))
    days = [dt.date(2026, 7, 6) + dt.timedelta(days=i) for i in range(7)]
    ws = FakeWS(days)
    s = c.update(ws, options, TZ, dt.date(2026, 7, 13))
    assert s["recomputed"] == 7 and len(ws.calls) == 2 and s["latest_week"]["zones"]["central"]["kwh_per_cdd"] == pytest.approx(1.2)
    assert (tmp_path / "controller" / "inputs" / "2026-07-06.json").exists()
    ws.calls.clear()
    s = c.update(ws, {**options, "controller_arm": "setback2"}, TZ, dt.date(2026, 7, 13))
    assert s["recomputed"] == 0 and ws.calls == [], "a complete day is not fetched again"
    assert {r["arm"] for r in c.days()} == {"baseline"}
    s = c.update(FakeWS([*days, dt.date(2026, 7, 13)]), {**options, "controller_arm": "setback2"}, TZ, dt.date(2026, 7, 14))
    assert c.days()[-1]["arm"] == "setback2" and c.weeks()[-1]["arms"] == ["setback2"]


def test_configuration_and_timestamps():
    assert ctl.parse_zones(["a=x,y", "b=z"]) == {"a": ["x", "y"], "b": ["z"]}
    for bad in (["a="], ["=x"], ["ax"]):
        with pytest.raises(ctl.ConfigError):
            ctl.parse_zones(bad)
    with pytest.raises(ctl.ConfigError):
        ctl.Controller.update(ctl.Controller.__new__(ctl.Controller), None, {}, TZ, dt.date(2026, 7, 1))
    assert ctl.hour_start(1782910800000.0) == 1782910800
    assert ctl.hour_start("2026-07-01T05:00:00+00:00") == int(dt.datetime(2026, 7, 1, 5, tzinfo=dt.UTC).timestamp())
