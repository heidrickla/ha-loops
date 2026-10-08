"""Loop 7, controller tuning, first slice: HVAC energy per cooling degree-day, by zone.

Measurement only: nothing here changes a thermostat. Each local day gets the hourly energy change of
every circuit of each zone and the hourly mean outdoor temperature from Core's long-term statistics.
CDD65 and HDD65 are degree-hours over 24. A day counts toward the ratio when it is a cooling day
(CDD65 >= 5, HDD65 < 1) and complete: every expected local hour (23 or 25 across a clock change)
holds the temperature and each of the zone's circuits. A week's ratio is a ratio of sums over at least
five such days. A missing hour makes its zone-day unavailable, never zero. `controller_arm` labels the
controller setting each day ran under, for A/B weeks the owner chooses.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

BASE_F = 65.0
COOLING_CDD = 5.0
COOLING_HDD_BELOW = 1.0
WEEK_MIN_DAYS = 5
HOUR = 3600


class ConfigError(ValueError):
    pass


def parse_zones(entries: list[str]) -> dict[str, list[str]]:
    """`name=statistic_id,statistic_id` per entry."""
    zones: dict[str, list[str]] = {}
    for entry in entries or []:
        name, _, ids = (entry or "").partition("=")
        stats = [s.strip() for s in ids.split(",") if s.strip()]
        if not name.strip() or not stats:
            raise ConfigError(f"hvac_zones entry {entry!r} is not name=statistic_id[,statistic_id]")
        zones[name.strip()] = stats
    return zones


def hour_start(value) -> int:
    """A statistics row's start as epoch seconds: milliseconds since 2023.3, an ISO string before."""
    if isinstance(value, int | float):
        return int(value / 1000) if value > 1e11 else int(value)
    return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def expected_hours(day: dt.date, tz: ZoneInfo) -> list[int]:
    start = dt.datetime.combine(day, dt.time(0), tz).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tz).astimezone(dt.UTC)
    return list(range(int(start.timestamp()), int(end.timestamp()), HOUR))


def fetch(ws, energy_ids: list[str], temp_id: str, start: dt.datetime, end: dt.datetime) -> dict[str, dict[int, float]]:
    """Hourly energy change in kWh and mean temperature in °F, keyed by statistic id then hour."""
    out: dict[str, dict[int, float]] = {}
    for ids, kind, units in ((energy_ids, "change", {"energy": "kWh"}), ([temp_id], "mean", {"temperature": "°F"})):
        got = ws.call(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=ids,
            period="hour",
            types=[kind],
            units=units,
        )
        for sid in ids:
            out[sid] = {hour_start(r["start"]): float(r[kind]) for r in (got or {}).get(sid, []) if r.get(kind) is not None}
    return out


def day_row(day: dt.date, tz: ZoneInfo, zones: dict[str, list[str]], temp_id: str, hours: dict[str, dict[int, float]], arm: str, meter_id: str = "") -> dict:
    """With `meter_id`, the panel's total energy: an hour it did not move is an hour the meter was not
    reporting (a house never uses 0 Wh in an hour), and every circuit's reading for it is missing.
    Statistics keep writing rows of zero change while the meter is offline."""
    expected = expected_hours(day, tz)
    temps = hours.get(temp_id, {})
    t_missing = [h for h in expected if h not in temps]
    meter = hours.get(meter_id, {}) if meter_id else None
    dead = {h for h in expected if meter is not None and meter.get(h, 0.0) <= 0.0}
    row: dict = {"date": day.isoformat(), "arm": arm, "expected_hours": len(expected), "temperature": {"statistic_id": temp_id, "present": len(expected) - len(t_missing), "missing": len(t_missing)}}
    if meter_id:
        row["meter"] = {"statistic_id": meter_id, "not_reporting": len(dead)}
    if t_missing:
        row.update(cdd65=None, hdd65=None, eligible=None, reason=f"outdoor temperature missing {len(t_missing)} of {len(expected)} hours")
    else:
        cdd = sum(max(temps[h] - BASE_F, 0.0) for h in expected) / 24
        hdd = sum(max(BASE_F - temps[h], 0.0) for h in expected) / 24
        eligible = cdd >= COOLING_CDD and hdd < COOLING_HDD_BELOW
        row.update(cdd65=round(cdd, 3), hdd65=round(hdd, 3), eligible=eligible, reason="cooling day" if eligible else f"shoulder or heating day (CDD65 {cdd:.2f}, HDD65 {hdd:.2f})")
    row["zones"] = {}
    for name, ids in zones.items():
        circuits = {}
        for sid in ids:
            got = hours.get(sid, {})
            missing = [h for h in expected if h not in got or h in dead]
            circuits[sid] = {"present": len(expected) - len(missing), "missing": len(missing), "kwh": None if missing else round(sum(got[h] for h in expected), 4)}
        complete = all(c["kwh"] is not None for c in circuits.values())
        row["zones"][name] = {"circuits": circuits, "complete": complete, "kwh": round(sum(c["kwh"] for c in circuits.values()), 4) if complete else None}
    return row


def counts(row: dict, zone: str) -> bool:
    z = row["zones"].get(zone)
    return bool(row.get("eligible")) and bool(z and z["complete"])


def week_rows(days: list[dict], zones: list[str]) -> list[dict]:
    """ISO weeks, local Monday to Sunday, newest last. A zone's ratio needs WEEK_MIN_DAYS counting days;
    the house total uses the days every zone counts."""
    weeks: dict[str, list[dict]] = {}
    for row in days:
        y, w, _ = dt.date.fromisoformat(row["date"]).isocalendar()
        weeks.setdefault(f"{y}-W{w:02d}", []).append(row)
    out = []
    for key in sorted(weeks):
        rows = sorted(weeks[key], key=lambda r: r["date"])
        week: dict = {"week": key, "first": rows[0]["date"], "last": rows[-1]["date"], "days": len(rows), "arms": sorted({r["arm"] for r in rows}), "zones": {}}
        for name in [*zones, "house"]:
            used = [r for r in rows if (all(counts(r, z) for z in zones) if name == "house" else counts(r, name))]
            kwh = sum(r["zones"][z]["kwh"] for r in used for z in (zones if name == "house" else [name]))
            cdd = sum(r["cdd65"] for r in used)
            if len(used) < WEEK_MIN_DAYS or cdd <= 0:
                week["zones"][name] = {"days_counted": len(used), "kwh": round(kwh, 3), "cdd65": round(cdd, 3), "kwh_per_cdd": None, "reason": f"{len(used)} counting days, {WEEK_MIN_DAYS} needed"}
            else:
                week["zones"][name] = {"days_counted": len(used), "kwh": round(kwh, 3), "cdd65": round(cdd, 3), "kwh_per_cdd": round(kwh / cdd, 4), "reason": ""}
        out.append(week)
    return out


class Controller:
    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "controller")
        os.makedirs(os.path.join(self.dir, "inputs"), exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _write(self, name: str, doc) -> None:
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self._path(name))

    def days(self) -> list[dict]:
        if not os.path.exists(self._path("days.json")):
            return []
        with open(self._path("days.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def weeks(self) -> list[dict]:
        if not os.path.exists(self._path("weeks.json")):
            return []
        with open(self._path("weeks.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def update(self, ws, options: dict, tz: ZoneInfo, today: dt.date) -> dict:
        """Fetch and compute every day from `controller_from` to yesterday that is not yet complete.
        Returns a summary for the nightly finding."""
        zones = parse_zones(options.get("hvac_zones") or [])
        temp_id = (options.get("outdoor_temperature") or "").strip()
        meter_id = (options.get("energy_meter") or "").strip()
        if not zones or not temp_id:
            raise ConfigError("hvac_zones and outdoor_temperature are not set")
        arm = (options.get("controller_arm") or "baseline").strip()
        first = dt.date.fromisoformat(options.get("controller_from") or (today - dt.timedelta(days=30)).isoformat())
        known = {r["date"]: r for r in self.days()}

        def done(r: dict | None) -> bool:
            same = bool(r) and set(r["zones"]) == set(zones) and (r.get("meter") or {}).get("statistic_id", "") == meter_id
            return same and r["temperature"]["missing"] == 0 and all(z["complete"] for z in r["zones"].values())

        todo = [first + dt.timedelta(days=i) for i in range((today - first).days) if not done(known.get((first + dt.timedelta(days=i)).isoformat()))]
        if todo:
            start = dt.datetime.combine(todo[0], dt.time(0), tz)
            end = dt.datetime.combine(todo[-1] + dt.timedelta(days=1), dt.time(0), tz)
            ids = sorted({s for v in zones.values() for s in v} | ({meter_id} if meter_id else set()))
            hours = fetch(ws, ids, temp_id, start, end)
            for day in todo:
                expected = set(expected_hours(day, tz))
                kept = {sid: {str(h): v for h, v in got.items() if h in expected} for sid, got in hours.items()}
                self._write(os.path.join("inputs", f"{day.isoformat()}.json"), kept)
                # a recomputed day keeps the arm it was first recorded under
                known[day.isoformat()] = day_row(day, tz, zones, temp_id, hours, known.get(day.isoformat(), {}).get("arm", arm), meter_id)
        rows = [known[k] for k in sorted(known)]
        weeks = week_rows(rows, list(zones))
        self._write("days.json", rows)
        self._write("weeks.json", weeks)
        measured = [w for w in weeks if any(z["kwh_per_cdd"] is not None for z in w["zones"].values())]
        return {
            "recomputed": len(todo),
            "days": len(rows),
            "latest_day": rows[-1] if rows else None,
            "latest_week": weeks[-1] if weeks else None,
            "latest_measured_week": measured[-1] if measured else None,
        }
