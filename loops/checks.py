"""The check suite. Each check reads one thing through the Supervisor or Home Assistant API
and returns findings; a finding that is fixed leaves its check behind, so the suite only grows.

A finding: {"check", "severity": info|warn|action, "title", "detail", "value"}.
Every check is read-only. Standard library only.
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import json
import re
import urllib.error
import urllib.request

SUPERVISOR = "http://supervisor"
LOG_LINE = re.compile(r"^\S+ \S+ (\w+) \(([\w-]+)\) \[([\w.-]+)\] (.*)$")
METRIC = re.compile(r"^monitor_status\{(.*)\} (\S+)$")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


class Api:
    """Supervisor and core reads with the add-on's own token."""

    def __init__(self, token: str, base: str = SUPERVISOR, timeout: int = 60):
        self.token = token
        self.base = base
        self.timeout = timeout

    def get(self, path: str, raw: bool = False):
        req = urllib.request.Request(f"{self.base}{path}", headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
        if raw:
            return body.decode("utf-8", "replace")
        doc = json.loads(body)
        return doc.get("data", doc) if isinstance(doc, dict) else doc

    def core(self, path: str):
        return self.get(f"/core/api{path}")

    def post_core(self, path: str, body: dict) -> int:
        req = urllib.request.Request(
            f"{self.base}/core/api{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code


def finding(check: str, severity: str, title: str, detail: str = "", value=None) -> dict:
    return {"check": check, "severity": severity, "title": title, "detail": detail, "value": value}


# ------------------------------------------------------------------ checks


def core_version(api: Api) -> list[dict]:
    info = api.get("/core/info")
    if info.get("update_available"):
        return [
            finding(
                "core_version",
                "info",
                f"Home Assistant {info.get('version')} -> {info.get('version_latest')} available",
                "Read the release post before applying; back up first.",
                info.get("version_latest"),
            )
        ]
    return []


def addon_updates(api: Api) -> list[dict]:
    addons = api.get("/addons").get("addons", [])
    out = []
    for a in addons:
        if a.get("update_available"):
            out.append(finding("addon_updates", "info", f"Add-on {a.get('name')} {a.get('version')} -> {a.get('version_latest')}", a.get("slug", ""), a.get("slug")))
    return out


def supervisor_issues(api: Api) -> list[dict]:
    info = api.get("/resolution/info")
    out = []
    for issue in info.get("issues", []):
        out.append(finding("supervisor_issues", "action", f"Supervisor issue: {issue.get('type')} on {issue.get('context')}", str(issue.get("reference") or ""), issue.get("uuid")))
    for item in info.get("unhealthy", []):
        out.append(finding("supervisor_issues", "action", f"Supervisor unhealthy: {item}", "", item))
    for item in info.get("unsupported", []):
        out.append(finding("supervisor_issues", "warn", f"Supervisor unsupported: {item}", "", item))
    return out


def backup_age(api: Api, max_age_days: int, now: dt.datetime) -> list[dict]:
    backups = api.get("/backups").get("backups", [])
    if not backups:
        return [finding("backup_age", "action", "No backups exist", "ha backups new --filename <name>.tar", None)]
    newest = max(dt.datetime.fromisoformat(b["date"].replace("Z", "+00:00")) for b in backups if b.get("date"))
    age = (now - newest).total_seconds() / 86400
    if age > max_age_days:
        return [finding("backup_age", "action", f"Newest backup is {age:.1f} days old", f"limit {max_age_days} days; {len(backups)} backups kept", round(age, 1))]
    return []


def disk(api: Api, warn_percent: int) -> list[dict]:
    host = api.get("/host/info")
    used, total = host.get("disk_used"), host.get("disk_total")
    if used and total:
        pct = 100.0 * used / total
        if pct > warn_percent:
            return [finding("disk", "warn", f"Host disk {pct:.0f}% used", f"{used:.1f} of {total:.1f} GB", round(pct, 1))]
    return []


def unavailable_entities(api: Api, previous: set[str] | None, warn_delta: int) -> tuple[list[dict], set[str]]:
    states = api.core("/states")
    current = {s["entity_id"] for s in states if s.get("state") in ("unavailable", "unknown")}
    out = []
    if previous is not None:
        new = sorted(current - previous)
        if len(current) - len(previous) >= warn_delta:
            out.append(
                finding("unavailable_entities", "warn", f"{len(current)} entities unavailable or unknown, {len(current) - len(previous)} more than the last run", ", ".join(new[:20]), len(current))
            )
        elif new:
            out.append(finding("unavailable_entities", "info", f"{len(new)} entities newly unavailable or unknown", ", ".join(new[:20]), len(new)))
    else:
        out.append(finding("unavailable_entities", "info", f"{len(current)} of {len(states)} entities unavailable or unknown", "first run; deltas start tomorrow", len(current)))
    return out, current


# Each check below reports only what is new since the previous run, so a known-dead sensor does not
# re-nag.
BATTERY_LOW = 20
SAFETY_CLASSES = {"smoke", "gas", "carbon_monoxide", "moisture", "safety", "problem", "door", "window", "connectivity", "battery"}
REF_DOMAINS = {
    "alarm_control_panel",
    "binary_sensor",
    "sensor",
    "switch",
    "light",
    "lock",
    "device_tracker",
    "update",
    "button",
    "number",
    "select",
    "climate",
    "cover",
    "fan",
    "media_player",
    "person",
    "zone",
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "automation",
    "script",
    "scene",
}
REF = re.compile(r"\b([a-z_]+)\.([a-z0-9_]+)\b")
SERVICE_OBJ = re.compile(
    r"^(turn_on|turn_off|toggle|reload|set_.*|.*_set|open_cover|close_cover|stop_cover|lock|unlock|arm_.*|alarm_.*|"
    r"disarm|trigger|press|select_.*|install|increment|decrement|start|stop|create|remove|update|apply|send_.*|.*_command)$"
)


def _new(check: str, severity: str, noun: str, current: dict[str, str], previous: dict[str, str] | None) -> list[dict]:
    """One finding for the keys whose value is new or changed since the previous run."""
    new = {k: v for k, v in current.items() if previous is None or previous.get(k) != v}
    if not new:
        return []
    items = [f"{k} ({v})" if v else k for k, v in sorted(new.items())]
    first = " (first run: everything current)" if previous is None else ""
    return [finding(check, severity, f"{len(new)} {noun}{first}", ", ".join(items[:20]), sorted(new))]


def device_health(api: Api, previous: dict | None) -> tuple[list[dict], dict]:
    """Safety-class devices and locks gone unavailable, batteries under 20 %, and pending updates that
    core_version and addon_updates do not already report."""
    offline, low, updates = {}, {}, {}
    # A local add-on's update entity has no hassio icon, so add-ons are matched by name as well.
    addons = {a.get("name") for a in (api.get("/addons") or {}).get("addons", [])}
    for s in api.core("/states"):
        eid, st, attr = s.get("entity_id", ""), s.get("state", ""), s.get("attributes") or {}
        if st == "unavailable" and (attr.get("device_class") in SAFETY_CLASSES or eid.startswith("lock.")):
            offline[eid] = ""
        if attr.get("device_class") == "battery" or eid.endswith("_battery"):
            try:
                if float(st) < BATTERY_LOW:
                    low[eid] = f"{st}%"
            except ValueError:
                pass
        picture = str(attr.get("entity_picture") or "")
        supervised = eid.startswith("update.home_assistant_") or "/api/hassio/addons/" in picture or attr.get("title") in addons
        if eid.startswith("update.") and st == "on" and not supervised:
            updates[eid] = f"{attr.get('title') or attr.get('friendly_name') or eid} -> {attr.get('latest_version', '?')}"

    def before(key: str) -> dict | None:
        return None if previous is None else previous.get(key, {})

    out = _new("safety_devices", "action", "safety devices or locks newly unavailable", offline, before("offline"))
    out += _new("battery_low", "warn", f"batteries newly under {BATTERY_LOW}%", low, before("low_battery"))
    out += _new("pending_updates", "info", "updates newly available", updates, before("updates"))
    return out, {"offline": offline, "low_battery": low, "updates": updates}


HARNESS_KEYS = {"version_regressed", "window_regressed"}


def harness_regressions(api: Api, previous: dict | None) -> tuple[list[dict], dict]:
    """Agent Harness Performance Tracker regression sensors that are on, found by the attributes only
    that integration sets. The value names the gates that fired, not their figures, so a regression
    that holds is not reported again each night."""
    regressed = {}
    for s in api.core("/states"):
        eid, attr = s.get("entity_id", ""), s.get("attributes") or {}
        if not eid.startswith("binary_sensor.") or not attr.keys() >= HARNESS_KEYS or s.get("state") != "on":
            continue
        why = [gate for gate in ("version", "window") if attr.get(f"{gate}_regressed")]
        tasks = sorted(str(t) for t in attr.get("regressed_tasks") or [])
        if tasks:
            why.append("tasks " + ", ".join(tasks[:5]))
        regressed[eid] = "; ".join(why) or "on"
    return _new("harness_regressed", "warn", "agent harnesses newly regressed", regressed, previous), regressed


def broken_refs(api: Api, previous: list[str] | None) -> tuple[list[dict], list[str]]:
    """Entity-shaped tokens in automation and script configs that name no live entity. Candidates for
    review, never fixed here: the match is textual, so a few are false positives."""
    states = api.core("/states")
    live = {s["entity_id"] for s in states}
    refs: set[str] = set()
    read = 0
    for s in states:
        eid, attr = s["entity_id"], s.get("attributes") or {}
        if eid.startswith("automation.") and attr.get("id"):
            text = json.dumps(api.core(f"/config/automation/config/{attr['id']}"))
        elif eid.startswith("script."):
            text = json.dumps(api.core(f"/config/script/config/{eid.split('.', 1)[1]}"))
        else:
            continue
        read += 1
        refs |= {f"{d}.{o}" for d, o in REF.findall(text) if d in REF_DOMAINS and not SERVICE_OBJ.match(o)}
    # Silence must mean "looked and found nothing": no config read, or configs with no entity in any
    # of them, is a failure to look.
    if any(s["entity_id"].startswith(("automation.", "script.")) for s in states) and not (read and refs):
        raise RuntimeError(f"read {read} automation or script configs and found {len(refs)} entity references")
    broken = sorted(refs - live)
    out = _new("broken_refs", "warn", "automation or script references to entities that do not exist", dict.fromkeys(broken, ""), dict.fromkeys(previous, "") if previous is not None else None)
    return out, broken


# One recurring error is one signature: the varying parts of a message are replaced.
SIGNATURE_SUBS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I), "<mac>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<s>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b", re.I), "<h>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def signature(level: str, logger: str, message: str) -> str:
    for pattern, token in SIGNATURE_SUBS:
        message = pattern.sub(token, message)
    return f"{level} {logger}: {message[:160]}"


def snapshot(api: Api, lines: int = 2000) -> dict:
    """Unavailable or unknown entities and ERROR/WARNING log signatures, read now.

    The deploy check (ha-management tools/ha_deploy_check.py) takes one before a change and two
    after it, and reports only what is new in both after-readings (from gstack's canary)."""
    states = api.core("/states")
    text = api.get(f"/core/logs?lines={lines}", raw=True)
    sigs: collections.Counter = collections.Counter()
    for raw in text.splitlines():
        m = LOG_LINE.match(re.sub(r"\x1b\[[0-9;]*m", "", raw).rstrip())
        if m and m.group(1) in ("ERROR", "WARNING"):
            sigs[signature(m.group(1), m.group(3), m.group(4))] += 1
    return {
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "entities": len(states),
        "unavailable": sorted(s["entity_id"] for s in states if s.get("state") in ("unavailable", "unknown")),
        "log_signatures": dict(sigs),
    }


def core_log_errors(api: Api, lines: int) -> list[dict]:
    text = api.get(f"/core/logs?lines={lines}", raw=True)
    levels: collections.Counter = collections.Counter()
    loggers: collections.Counter = collections.Counter()
    for raw in text.splitlines():
        m = LOG_LINE.match(re.sub(r"\x1b\[[0-9;]*m", "", raw).rstrip())
        if not m:
            continue
        level, _thread, logger, _msg = m.groups()
        if level in ("ERROR", "WARNING"):
            levels[level] += 1
            loggers[logger] += 1
    if not levels:
        return []
    top = ", ".join(f"{name} x{n}" for name, n in loggers.most_common(5))
    severity = "warn" if levels["ERROR"] else "info"
    return [finding("core_log_errors", severity, f"Core log: {levels['ERROR']} errors, {levels['WARNING']} warnings in the last {lines} lines", top, dict(levels))]


def down_monitors(metrics: str, ignore: frozenset[str] = frozenset()) -> list[str]:
    """Monitors whose status is 0, minus `ignore`: monitors of machines that are off unless a job
    needs them, where down is the normal state."""
    down = []
    for line in metrics.splitlines():
        m = METRIC.match(line)
        if m and m.group(2) == "0":
            name = dict(LABEL.findall(m.group(1))).get("monitor_name", "?")
            if name not in ignore:
                down.append(name)
    return down


def kuma_down(url: str, key: str, ignore: frozenset[str] = frozenset()) -> list[dict]:
    if not url or not key:
        return []
    auth = base64.b64encode(f":{key}".encode()).decode()
    req = urllib.request.Request(f"{url.rstrip('/')}/metrics", headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    down = down_monitors(text, ignore)
    return [finding("kuma_down", "action", f"Uptime Kuma: {len(down)} monitors down", ", ".join(down), down)] if down else []


def run_all(api: Api, options: dict, state: dict, now: dt.datetime) -> tuple[list[dict], dict]:
    """Every check, each failure of a check becoming its own finding rather than stopping the run."""
    findings: list[dict] = []
    new_state = dict(state)

    def guard(name, fn, *args):
        try:
            return fn(*args)
        except Exception as err:  # a check that cannot read is a finding about the check
            return [finding(name, "warn", f"check {name} failed: {type(err).__name__}: {err}", "", None)]

    findings += guard("core_version", core_version, api)
    findings += guard("addon_updates", addon_updates, api)
    findings += guard("supervisor_issues", supervisor_issues, api)
    findings += guard("backup_age", backup_age, api, int(options.get("backup_max_age_days") or 7), now)
    findings += guard("disk", disk, api, int(options.get("disk_warn_percent") or 85))
    try:
        previous = set(state.get("unavailable") or []) if state.get("unavailable") is not None else None
        out, current = unavailable_entities(api, previous, int(options.get("unavailable_warn_delta") or 25))
        findings += out
        new_state["unavailable"] = sorted(current)
    except Exception as err:
        findings.append(finding("unavailable_entities", "warn", f"check unavailable_entities failed: {type(err).__name__}: {err}"))
    for name, fn in (("device_health", device_health), ("broken_refs", broken_refs), ("harness_regressions", harness_regressions)):
        try:
            out, current = fn(api, state.get(name))
            findings += out
            new_state[name] = current
        except Exception as err:  # state kept as it was, so the next run still compares against a real one
            findings.append(finding(name, "warn", f"check {name} failed: {type(err).__name__}: {err}"))
    findings += guard("core_log_errors", core_log_errors, api, int(options.get("log_lines") or 500))
    ignore = frozenset(n for n in options.get("kuma_ignore") or [] if n)
    findings += guard("kuma_down", kuma_down, options.get("kuma_url") or "", options.get("kuma_api_key") or "", ignore)
    return findings, new_state
