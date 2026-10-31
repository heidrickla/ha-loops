"""The check suite and the override corpus against a fake Supervisor; the server end to end."""

from __future__ import annotations

import collections
import datetime as dt
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import checks
import overrides as ov
import server as srv

NOW = dt.datetime(2026, 9, 20, 3, 30, tzinfo=dt.UTC)
T0 = "2026-09-19T10:00:00+00:00"
T1 = "2026-09-19T10:01:00+00:00"
T2 = "2026-09-19T10:20:00+00:00"

LOGBOOK = [
    {"when": T0, "entity_id": "light.hall", "state": "on", "context_domain": "automation", "context_entity_id": "automation.hall_motion", "context_entity_id_name": "Hall motion"},
    {"when": T1, "entity_id": "light.hall", "state": "off", "context_user_id": "afe0089f638c474293cfc56280e435f1", "context_domain": "light"},
    {"when": T2, "entity_id": "light.hall", "state": "off", "context_user_id": "afe0089f638c474293cfc56280e435f1"},
    {"when": T0, "entity_id": "automation.hall_motion", "domain": "automation", "name": "Hall motion", "state": None, "message": "triggered"},
    {"when": T0, "entity_id": "switch.fan", "state": "on", "context_domain": "automation", "context_entity_id": "automation.fan"},
]

SUPERVISOR = {
    "/core/info": {"version": "2026.9.1", "version_latest": "2026.9.2", "update_available": True},
    "/supervisor/info": {
        "version": "2026.10.5",
        "addons": [
            {"name": "Vaultwarden", "slug": "a0d7b954_bitwarden", "version": "0.28.2", "version_latest": "0.28.3", "update_available": True, "state": "started"},
            {"name": "SSH", "slug": "core_ssh", "update_available": False, "state": "started"},
        ],
    },
    "/network/info": {"interfaces": [{"interface": "end0", "primary": True, "ipv4": {"gateway": "127.0.0.1"}}]},
    "/resolution/info": {
        "issues": [
            {"type": "free_space", "context": "system", "reference": None, "uuid": "u1"},
            {"type": "no_current_backup", "context": "system", "reference": None, "uuid": "u2"},
        ],
        "unhealthy": [],
        "unsupported": ["os_agent"],
    },
    "/backups/info": {
        "backups": [
            {"date": "2026-09-01T00:00:00.000000+00:00", "type": "full", "content": {"homeassistant": True}},
            {"date": "2026-09-10T02:00:00.000000+00:00", "type": "partial", "content": {"homeassistant": True}},
            # newer, but one add-on only: no restore point for Home Assistant
            {"date": "2026-09-19T02:00:00.000000+00:00", "type": "partial", "content": {"homeassistant": False, "addons": ["local_loops"]}},
        ]
    },
    "/host/info": {"disk_used": 44.0, "disk_total": 50.0},
    "/core/api/states": [{"entity_id": "light.a", "state": "on"}, {"entity_id": "sensor.b", "state": "unavailable"}, {"entity_id": "sensor.c", "state": "unknown"}],
    "/core/api/config": {"time_zone": "America/Chicago"},
}
CORE_LOG = "2026-09-19 03:12:01.123 ERROR (Thread-7) [pychromecast.socket_client] Failed\n2026-09-19 03:12:02.000 WARNING (MainThread) [homeassistant.components.zha] slow\n2026-09-19 03:12:03.000 INFO (MainThread) [homeassistant.setup] fine\n"


class FakeSupervisor(BaseHTTPRequestHandler):
    posted: ClassVar[list] = []
    paths: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        FakeSupervisor.paths.append(path)
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_response(401)
            self.end_headers()
            return
        if path == "/core/logs":
            body, ctype = CORE_LOG.encode(), "text/plain"
        elif path.startswith("/core/api/logbook/"):
            body, ctype = json.dumps(LOGBOOK).encode(), "application/json"
        elif path in SUPERVISOR:
            doc = SUPERVISOR[path]
            body, ctype = json.dumps(doc if path.startswith("/core/api") else {"result": "ok", "data": doc}).encode(), "application/json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        FakeSupervisor.posted.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")


@pytest.fixture
def supervisor():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSupervisor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeSupervisor.posted.clear()
    FakeSupervisor.paths.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def options(**extra) -> dict:
    return {
        "run_at": "03:30",
        "notify_target": "mobile_app_test",
        "override_window_s": 300,
        "backup_max_age_days": 7,
        "disk_warn_percent": 85,
        "unavailable_warn_delta": 1,
        "log_lines": 500,
        **extra,
    }


def test_checks_find_each_planted_problem(supervisor):
    api = checks.Api("test-token", base=supervisor)
    findings, state = checks.run_all(api, options(), {}, NOW)
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    assert by_check["core_version"][0]["value"] == "2026.9.2"
    assert by_check["addon_updates"][0]["value"] == "a0d7b954_bitwarden" and len(by_check["addon_updates"]) == 1
    severity = {f["value"]: f["severity"] for f in by_check["supervisor_issues"]}
    assert severity == {"u1": "action", "u2": "info", "os_agent": "warn"}
    assert by_check["backup_age"][0]["severity"] == "action" and by_check["backup_age"][0]["value"] == 10.1
    assert by_check["disk"][0]["value"] == 88.0
    assert by_check["unavailable_entities"][0]["value"] == 2 and state["unavailable"] == ["sensor.b", "sensor.c"]
    assert by_check["core_log_errors"][0]["value"] == {"ERROR": 1, "WARNING": 1}
    assert "kuma_down" not in by_check  # not configured, not a finding


def test_signature_folds_the_varying_parts():
    a = checks.signature("ERROR", "x.y", "Timeout after 30.5 s talking to 192.168.1.2:8080 for 'kitchen'")
    b = checks.signature("ERROR", "x.y", "Timeout after 12 s talking to 192.168.1.9:80 for 'hall'")
    assert a == b == "ERROR x.y: Timeout after <n> s talking to <ip> for <s>"
    assert checks.signature("ERROR", "x.y", "Other failure") != a


def test_snapshot_reads_unavailable_and_log_signatures(supervisor):
    snap = checks.snapshot(checks.Api("test-token", base=supervisor))
    assert snap["entities"] == 3
    assert snap["unavailable"] == ["sensor.b", "sensor.c"]
    assert snap["log_signatures"] == {
        "ERROR pychromecast.socket_client: Failed": 1,
        "WARNING homeassistant.components.zha: slow": 1,
    }


def test_snapshot_counts_what_core_logged_while_it_stopped(supervisor, monkeypatch):
    log = "\n".join(
        [
            # A run: the config-check warning 20 s before the stop, a poll racing the closed session.
            "2026-10-01 08:37:59.623 WARNING (SyncWorker_6) [homeassistant.loader] custom integration x",
            "2026-10-01 08:38:18.398 ERROR (MainThread) [homeassistant.components.unifi] Unexpected error fetching UniFi TrafficRules data",
            "Traceback (most recent call last):",
            "RuntimeError: Session is closed",
            "s6-rc: info: service legacy-services: stopping",
            "[13:38:22] INFO: Home Assistant Core finish process exit code 0",
            # The next run logs the same error while running: that one is not the stop's.
            "2026-10-01 08:38:27.179 WARNING (SyncWorker_0) [homeassistant.loader] custom integration x",
            "2026-10-01 09:00:00.000 ERROR (MainThread) [homeassistant.components.unifi] Unexpected error fetching UniFi TrafficRules data",
            "",
        ]
    )
    monkeypatch.setattr(sys.modules[__name__], "CORE_LOG", log)
    snap = checks.snapshot(checks.Api("test-token", base=supervisor))
    unifi = "ERROR homeassistant.components.unifi: Unexpected error fetching UniFi TrafficRules data"
    loader = "WARNING homeassistant.loader: custom integration x"
    assert snap["log_signatures"][unifi] == 2
    assert snap["stop_signatures"] == {unifi: 1}
    assert snap["log_signatures"][loader] == 2


def test_each_stop_counts_its_own_last_seconds(supervisor, monkeypatch):
    """Which occurrence is counted, not only how many: each signature differs."""
    log = "\n".join(
        [
            "2026-10-01 08:38:18.398 ERROR (MainThread) [first.stop] closing",
            # The marker in colour, then a second with no run between them.
            "\x1b[32m[13:38:22] INFO: Home Assistant Core finish process exit code 0\x1b[0m",
            "[13:38:23] INFO: Home Assistant Core finish process exit code 0",
            "2026-10-01 08:39:00.000 ERROR (MainThread) [while.running] poll failed",
            "2026-10-01 09:00:00.000 ERROR (MainThread) [second.stop] closing",
            "[14:00:01] INFO: Home Assistant Core finish process exit code 0",
            "2026-10-01 09:00:30.000 ERROR (MainThread) [after.restart] not ready",
            "",
        ]
    )
    monkeypatch.setattr(sys.modules[__name__], "CORE_LOG", log)
    snap = checks.snapshot(checks.Api("test-token", base=supervisor))
    assert snap["stop_signatures"] == {
        "ERROR first.stop: closing": 1,
        "ERROR second.stop: closing": 1,
    }
    assert len(snap["log_signatures"]) == 4


def test_a_log_with_no_exit_marker_has_no_stop_signatures(supervisor):
    snap = checks.snapshot(checks.Api("test-token", base=supervisor))
    assert snap["stop_signatures"] == {}


def test_unavailable_delta_uses_the_previous_run(supervisor):
    api = checks.Api("test-token", base=supervisor)
    findings, _ = checks.run_all(api, options(), {"unavailable": ["sensor.b"]}, NOW)
    f = next(x for x in findings if x["check"] == "unavailable_entities")
    assert f["severity"] == "warn" and "sensor.c" in f["detail"] and f["value"] == 2
    findings, _ = checks.run_all(api, options(unavailable_warn_delta=5), {"unavailable": ["sensor.b"]}, NOW)
    f = next(x for x in findings if x["check"] == "unavailable_entities")
    assert f["severity"] == "info" and f["value"] == 1


def test_a_failing_check_is_a_finding_not_a_crash(supervisor):
    api = checks.Api("wrong-token", base=supervisor)
    findings, _ = checks.run_all(api, options(), {}, NOW)
    assert findings and all("failed" in f["title"] for f in findings)


class StatesApi:
    """core() from a dict of paths and get("/supervisor/info") from a list of add-on names, without HTTP."""

    def __init__(self, docs: dict, addons: tuple = ("Loops",)):
        self.docs = docs
        self.addons = addons

    def core(self, path: str):
        value = self.docs[path]
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, path: str):
        assert path == "/supervisor/info"
        return {"addons": [{"name": n} for n in self.addons]}


HEALTH_STATES = [
    {"entity_id": "binary_sensor.kitchen_leak", "state": "unavailable", "attributes": {"device_class": "moisture"}},
    {"entity_id": "lock.front_door", "state": "unavailable", "attributes": {}},
    {"entity_id": "media_player.tv", "state": "unavailable", "attributes": {}},
    {"entity_id": "sensor.garage_door_battery", "state": "0", "attributes": {}},
    {"entity_id": "sensor.remote_battery", "state": "55", "attributes": {"device_class": "battery"}},
    {"entity_id": "sensor.flaky_battery", "state": "unknown", "attributes": {"device_class": "battery"}},
    {"entity_id": "update.room_summary_card_update", "state": "on", "attributes": {"title": "Room Summary Card", "latest_version": "2.4.0"}},
    {"entity_id": "update.home_assistant_core_update", "state": "on", "attributes": {"latest_version": "2026.10.0"}},
    {"entity_id": "update.vaultwarden_update", "state": "on", "attributes": {"entity_picture": "/api/hassio/addons/a0d7b954_bitwarden/icon"}},
    {"entity_id": "update.zha_fw", "state": "off", "attributes": {}},
    {"entity_id": "update.loops_update", "state": "on", "attributes": {"title": "Loops", "latest_version": "0.3.0"}},
]


def test_device_health_reports_only_whats_new():
    api = StatesApi({"/states": HEALTH_STATES})
    first, state = checks.device_health(api, None)
    by = {f["check"]: f for f in first}
    assert by["safety_devices"]["value"] == ["binary_sensor.kitchen_leak", "lock.front_door"] and by["safety_devices"]["severity"] == "action"
    assert by["battery_low"]["value"] == ["sensor.garage_door_battery"] and "first run" in by["battery_low"]["title"]
    # core and add-on updates are core_version's and addon_updates' findings, not this one's
    assert by["pending_updates"]["value"] == ["update.room_summary_card_update"]
    again, _ = checks.device_health(api, state)
    assert again == []
    newer = [dict(s) for s in HEALTH_STATES]
    newer[6] = {**newer[6], "attributes": {"title": "Room Summary Card", "latest_version": "2.5.0"}}
    changed, _ = checks.device_health(StatesApi({"/states": newer}), state)
    assert [f["check"] for f in changed] == ["pending_updates"] and "2.5.0" in changed[0]["detail"]


TRACKER_ATTRS = {"device_class": "problem", "version_regressed": False, "window_regressed": False, "regressed_tasks": []}
HARNESS_STATES = [
    {"entity_id": "binary_sensor.codex_harness_regressed", "state": "off", "attributes": TRACKER_ATTRS},
    {
        "entity_id": "binary_sensor.claude_code_harness_regressed",
        "state": "on",
        "attributes": {**TRACKER_ATTRS, "window_regressed": True, "regressed_tasks": ["hook-build", "ha-repairs"]},
    },
    # on, but not the tracker's: no window_regressed attribute
    {"entity_id": "binary_sensor.other_harness_regressed", "state": "on", "attributes": {"device_class": "problem", "version_regressed": True}},
]


def test_harness_regressions_report_the_trackers_sensors_when_newly_on():
    first, state = checks.harness_regressions(StatesApi({"/states": HARNESS_STATES}), None)
    assert [f["value"] for f in first] == [["binary_sensor.claude_code_harness_regressed"]]
    assert first[0]["severity"] == "warn" and "window; tasks ha-repairs, hook-build" in first[0]["detail"]
    # The same regression the next night is not news.
    again, _ = checks.harness_regressions(StatesApi({"/states": HARNESS_STATES}), state)
    assert again == []
    # A second gate firing is.
    worse = [dict(s) for s in HARNESS_STATES]
    worse[1] = {**worse[1], "attributes": {**worse[1]["attributes"], "version_regressed": True}}
    changed, _ = checks.harness_regressions(StatesApi({"/states": worse}), state)
    assert changed and "version; window" in changed[0]["detail"]


def test_run_all_carries_the_harness_state_between_runs(supervisor):
    api = checks.Api("test-token", base=supervisor)
    _, state = checks.run_all(api, options(), {}, NOW)
    assert state["harness_regressions"] == {}


def test_device_health_with_an_empty_previous_state_is_not_a_first_run():
    out, _ = checks.device_health(StatesApi({"/states": HEALTH_STATES}), {})
    assert out and not any("first run" in f["title"] for f in out)


def test_broken_refs_names_entities_that_do_not_exist():
    states = [
        {"entity_id": "automation.arrive", "state": "on", "attributes": {"id": "1665"}},
        {"entity_id": "automation.yaml_only", "state": "on", "attributes": {}},
        {"entity_id": "script.goodnight", "state": "off", "attributes": {}},
        {"entity_id": "lock.front_door", "state": "locked", "attributes": {}},
    ]
    docs = {
        "/states": states,
        "/config/automation/config/1665": {"triggers": [{"entity_id": "device_tracker.gone_phone"}], "actions": [{"action": "lock.unlock", "target": {"entity_id": "lock.front_door"}}]},
        "/config/script/config/goodnight": {"sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.retired_lamp"}}]},
    }
    out, broken = checks.broken_refs(StatesApi(docs), None)
    assert broken == ["device_tracker.gone_phone", "light.retired_lamp"]
    assert out[0]["severity"] == "warn" and out[0]["value"] == broken
    assert checks.broken_refs(StatesApi(docs), broken)[0] == []


def test_broken_refs_that_read_nothing_is_a_failure_not_a_clean_result():
    no_ids = {"/states": [{"entity_id": "automation.yaml_only", "state": "on", "attributes": {}}]}
    with pytest.raises(RuntimeError, match="read 0"):
        checks.broken_refs(StatesApi(no_ids), None)
    no_refs = {"/states": [{"entity_id": "script.noop", "state": "off", "attributes": {}}], "/config/script/config/noop": {"sequence": [{"delay": 1}]}}
    with pytest.raises(RuntimeError, match="found 0"):
        checks.broken_refs(StatesApi(no_refs), None)
    assert checks.broken_refs(StatesApi({"/states": [{"entity_id": "light.a", "state": "on"}]}), None) == ([], [])


def config_error(status: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"message": message}).encode()
    return urllib.error.HTTPError("http://core/config", status, "error", {}, io.BytesIO(body))


def test_broken_refs_skips_yaml_only_automation_and_scans_remaining_configs():
    docs = {
        "/states": [
            {"entity_id": "automation.yaml_only", "attributes": {"id": "yaml-id"}},
            {"entity_id": "script.goodnight", "attributes": {}},
        ],
        "/config/automation/config/yaml-id": config_error(404, "Not found"),
        "/config/script/config/goodnight": {"sequence": [{"entity_id": "light.retired_lamp"}]},
    }
    out, broken = checks.broken_refs(StatesApi(docs), None)
    assert broken == ["light.retired_lamp"]
    assert any("automation.yaml_only" in f["detail"] and "skipped 1" in f["detail"].lower() for f in out)
    assert any(f["value"] == broken for f in out)


def test_broken_refs_names_one_unreadable_config_and_continues():
    docs = {
        "/states": [
            {"entity_id": "automation.bad", "attributes": {"id": "bad"}},
            {"entity_id": "script.goodnight", "attributes": {}},
        ],
        "/config/automation/config/bad": config_error(500, "Secrets not supported in this YAML file"),
        "/config/script/config/goodnight": {"sequence": [{"entity_id": "light.retired_lamp"}]},
    }
    out, broken = checks.broken_refs(StatesApi(docs), None)
    assert broken == ["light.retired_lamp"]
    assert any("automation.bad" in f["title"] and "500" in f["title"] and "Secrets not supported" in f["title"] for f in out)
    assert any(f["value"] == broken for f in out)


@pytest.mark.parametrize("kind,file", [("automation", "automations.yaml"), ("script", "scripts.yaml")])
def test_broken_refs_groups_the_same_error_when_every_config_read_fails(kind, file):
    states = [{"entity_id": f"{kind}.{n}", "attributes": {"id": n}} for n in ("one", "two")]
    path = f"/config/{kind}/config/"
    docs = {"/states": states, **{path + n: config_error(500, "Secrets not supported in this YAML file") for n in ("one", "two")}}
    out, broken = checks.broken_refs(StatesApi(docs), None)
    assert broken == [] and len(out) == 1
    assert out[0]["severity"] == "warn" and "500" in out[0]["title"] and file in out[0]["title"]
    assert "Secrets not supported" in out[0]["title"]


def test_collector_setup_retries_core_until_it_starts(tmp_path, monkeypatch, caplog):
    class FlakyApi:
        calls = 0

        def core(self, path):
            assert path == "/config"
            self.calls += 1
            if self.calls <= 3:
                raise config_error(502, "Core starting")
            return {"time_zone": "America/Chicago"}

    api = FlakyApi()
    loops = srv.Loops(options(), str(tmp_path), api=api)
    stop = threading.Event()
    waits = []
    original_wait = stop.wait
    stop.wait = lambda seconds: waits.append(seconds) or original_wait(0)
    started = []

    def run(collector, passed_stop, connect):
        started.append((collector.tz.key, passed_stop, connect))
        stop.set()

    monkeypatch.setattr(srv.col.Collector, "run", run)
    with caplog.at_level("INFO"):
        loops.run_collector(stop)
    assert api.calls == 4 and waits == [10, 20, 40]
    assert started == [("America/Chicago", stop, loops.socket)]
    assert sum("collector setup failed" in r.message for r in caplog.records) == 1
    assert sum("collector setup recovered" in r.message for r in caplog.records) == 1


def test_collector_setup_backoff_is_capped_and_stops(tmp_path):
    class DownApi:
        calls = 0

        def core(self, path):
            self.calls += 1
            raise config_error(502, "Core starting")

    api = DownApi()
    loops = srv.Loops(options(), str(tmp_path), api=api)
    stop = threading.Event()
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 8:
            stop.set()
        return stop.is_set()

    stop.wait = wait
    loops.run_collector(stop)
    assert waits == [10, 20, 40, 80, 160, 300, 300, 300]
    assert api.calls == 8


def test_down_monitors_leaves_out_ignored_ones():
    metrics = 'monitor_status{monitor_name="Lab SSH",monitor_type="port"} 0\nmonitor_status{monitor_name="NetBox",monitor_type="http"} 0\nmonitor_status{monitor_name="Zabbix",monitor_type="http"} 1\n'
    assert checks.down_monitors(metrics) == ["Lab SSH", "NetBox"]
    assert checks.down_monitors(metrics, frozenset({"Lab SSH"})) == ["NetBox"]


def test_override_detection_and_ranking(tmp_path):
    found = ov.find_overrides(LOGBOOK, 300)
    assert len(found) == 1
    o = found[0]
    assert o["automation"] == "automation.hall_motion" and o["automation_set"] == "on" and o["person_set"] == "off" and o["seconds_later"] == 60.0
    assert ov.find_overrides(LOGBOOK, 30) == []  # outside the window
    corpus = ov.Corpus(str(tmp_path))
    corpus.append(found)
    corpus.add_fires("2026-09-19", ov.automation_fires(LOGBOOK))
    ranking = corpus.ranking(days=36500)
    assert ranking[0]["automation"] == "automation.hall_motion" and ranking[0]["overrides"] == 1 and ranking[0]["fired"] == 1 and ranking[0]["rate"] == 1.0
    assert ranking[0]["runs_undone"] == 1


def test_an_override_is_stored_and_counted_once(tmp_path):
    found = ov.find_overrides(LOGBOOK, 300)
    corpus = ov.Corpus(str(tmp_path))
    # A manual run between nightly ones reads the same day again.
    corpus.append(found)
    corpus.append(found)
    assert (tmp_path / "overrides.jsonl").read_text(encoding="utf-8").count("\n") == 1
    # Copies already on disk from before the fix count once.
    with open(tmp_path / "overrides.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(found[0]) + "\n")
    assert len(corpus.rows()) == 1
    corpus.add_fires("2026-09-19", ov.automation_fires(LOGBOOK))
    assert corpus.ranking(days=36500)[0]["overrides"] == 1


def _override(automation, entity, auto_at, later, base="2026-09-19T20:00:00+00:00"):
    t0 = dt.datetime.fromisoformat(base)
    when = t0 + dt.timedelta(seconds=auto_at + later)
    return {"when": when.isoformat(), "entity_id": entity, "automation": automation, "automation_name": automation, "automation_set": "on", "person_set": "off", "seconds_later": later, "user": "u1"}


def test_one_automation_run_undone_counts_once_whatever_it_changed(tmp_path):
    # One run of the bath automation turned on two lights; the person turned both off a few seconds apart.
    one_run = [_override("automation.bath", "light.vanity", 0, 30), _override("automation.bath", "light.ceiling", 0.4, 33)]
    assert ov.runs_undone(one_run) == 1
    # A later run undone again is a second run.
    assert ov.runs_undone([*one_run, _override("automation.bath", "light.vanity", 600, 20)]) == 2
    # A run whose actions are chained 4 s apart is still one run; a 6 s gap starts another.
    assert ov.runs_undone([_override("automation.bath", f"light.l{i}", 4 * i, 10) for i in range(3)]) == 1
    assert ov.runs_undone([_override("automation.bath", "light.a", 0, 10), _override("automation.bath", "light.b", 6, 10)]) == 2
    # Two automations changing things at the same moment are two runs.
    assert ov.runs_undone([_override("automation.bath", "light.a", 0, 10), _override("automation.hall", "light.b", 0, 10)]) == 2
    # The run time is the automation's change, not the person's: undone 1 s and 200 s later, the same run.
    assert ov.runs_undone([_override("automation.bath", "light.a", 0, 1), _override("automation.bath", "light.b", 0.2, 200)]) == 1
    # A row stored without seconds_later is read at its own time.
    bare = _override("automation.bath", "light.a", 0, 0)
    del bare["seconds_later"]
    assert ov.runs_undone([bare]) == 1 and ov.run_time(bare) == dt.datetime.fromisoformat(bare["when"])
    assert ov.runs_undone([]) == 0

    corpus = ov.Corpus(str(tmp_path))
    corpus.append([*one_run, _override("automation.hall", "light.hall", 100, 15), _override("automation.hall", "light.hall", 900, 15)])
    corpus.add_fires("2026-09-19", collections.Counter({"automation.bath": 4, "automation.hall": 10}))
    ranking = corpus.ranking(days=36500)
    # Ranked by runs undone: the hall's two runs beat the bath's one run with two changes.
    assert [a["automation"] for a in ranking] == ["automation.hall", "automation.bath"]
    bath = ranking[1]
    assert bath["runs_undone"] == 1 and bath["overrides"] == 2 and bath["rate"] == 0.25
    assert bath["entities"] == {"light.vanity": 1, "light.ceiling": 1} and "rows" not in bath
    assert ranking[0]["runs_undone"] == 2 and ranking[0]["rate"] == 0.2


def test_run_once_writes_findings_corpus_and_notifies(supervisor, tmp_path):
    loops = srv.Loops(options(), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    run = loops.run_once()
    assert run["counts"]["action"] >= 2 and run["overrides"] == 1 and run["runs_undone"] == 1
    assert (tmp_path / "findings" / "2026-09-20.json").exists() and (tmp_path / "latest.json").exists()
    assert json.loads((tmp_path / "state.json").read_text())["last_run"] == run["ran_at"]
    assert len(loops.store.history()) == 1
    assert FakeSupervisor.posted and FakeSupervisor.posted[0][0] == "/core/api/services/notify/mobile_app_test"
    assert "1 overrides yesterday" in FakeSupervisor.posted[0][1]["message"]


def test_server_serves_dark_page_and_json(supervisor, tmp_path):
    loops = srv.Loops(options(notify_target=""), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    http = srv.serve(loops, 0, host="127.0.0.1", fixed_peers=("127.0.0.1",))
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok\n"
        page = urllib.request.urlopen(base + "/")
        html = page.read().decode()
        assert page.headers["Content-Type"] == "text/html; charset=utf-8"
        assert '<html lang="en" data-theme="dark">' in html and "color-scheme: dark" in html
        # under ingress the page sits below /api/hassio_ingress/<token>/, so an absolute fetch misses
        assert '"api/latest.json"' in html and '"/api/' not in html
        assert json.loads(urllib.request.urlopen(base + "/api/latest.json").read()) is None
        req = urllib.request.Request(base + "/api/run", data=b"{}", method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["overrides"] == 1
        latest = json.loads(urllib.request.urlopen(base + "/api/latest.json").read())
        assert latest["counts"]["action"] >= 2
        assert any(f["title"].startswith("Newest backup") for f in latest["findings"])
        ranking = json.loads(urllib.request.urlopen(base + "/api/overrides.json").read())
        assert ranking[0]["name"] == "Hall motion" and ranking[0]["runs_undone"] == 1
        history = json.loads(urllib.request.urlopen(base + "/api/history.json").read())
        assert len(history) == 1 and history[0]["counts"] == latest["counts"]
    finally:
        http.shutdown()


def test_schedule_math():
    loops = srv.Loops(options(run_at="03:30"), ".", api=checks.Api("t", base="http://127.0.0.1:9"), now=lambda: NOW)
    before = dt.datetime(2026, 9, 20, 1, 0)
    after = dt.datetime(2026, 9, 20, 4, 0)
    assert loops.seconds_until_next_run(before) == 2.5 * 3600
    assert loops.seconds_until_next_run(after) == 23.5 * 3600


FAILED_ROW = {
    "entity_id": "automation.hall_motion",
    "name": "Hall motion",
    "unique_id": "1665",
    "last_error_time": "2026-09-19T22:00:00+00:00",
    "error_message": "Template variable error: 'None' has no attribute 'state'",
    "error_step": "action/1",
    "consecutive_failures": 1,
}
LINKED_ROW = {
    "entity_id": "light.office_desk",
    "name": "Office desk",
    "state": "unavailable",
    "device_id": None,
    "unavailable_since": "2026-09-19T20:00:00+00:00",
    "referenced_by": ["automation.office_on", "script.desk"],
}
RADAR_ATTRS = {
    "findings": [
        {"domain": "battery_notes", "breaks_in": "2027.8", "file": "custom_components/battery_notes/coordinator.py", "line": 740},
        {"domain": "envisalink_new", "breaks_in": "2027.8", "file": "custom_components/envisalink_new/sensor.py", "line": 30},
        {"domain": "envisalink_new", "breaks_in": "2026.11", "file": "custom_components/envisalink_new/__init__.py", "line": 12},
    ],
    "schedule": [],
    "broken_now": {},
    "imminent": {"envisalink_new": "2026.11"},
    "index_url": "https://booyaka101.github.io/hass-breakage-radar/index.json",
}
MONITOR_STATES = [
    {"entity_id": "sensor.failed_automations", "state": "1", "attributes": {"automations": [FAILED_ROW]}},
    {"entity_id": "sensor.linked_entities_unavailable", "state": "1", "attributes": {"entities": [LINKED_ROW]}},
    {"entity_id": "sensor.breakage_radar_affected", "state": "2", "attributes": RADAR_ATTRS},
    # an `automations` attribute of another shape is some other sensor's
    {"entity_id": "sensor.other", "state": "3", "attributes": {"automations": [{"entity_id": "automation.x"}]}},
]


def test_monitors_report_each_kind_once_and_a_change_again():
    out, state = checks.monitors(StatesApi({"/states": MONITOR_STATES}), {})
    by = {f["check"]: f for f in out}
    assert by["automation_failures"]["value"] == ["automation.hall_motion"] and "action/1" in by["automation_failures"]["detail"]
    assert by["linked_unavailable"]["value"] == ["light.office_desk"] and "used by 2" in by["linked_unavailable"]["detail"]
    assert by["breakage_imminent"]["severity"] == "warn" and by["breakage_imminent"]["value"] == ["envisalink_new"]
    assert "2026.11" in by["breakage_imminent"]["detail"]
    assert by["breakage_upcoming"]["severity"] == "info" and by["breakage_upcoming"]["value"] == ["battery_notes"]
    assert set(by) == {"automation_failures", "linked_unavailable", "breakage_imminent", "breakage_upcoming"}
    again, _ = checks.monitors(StatesApi({"/states": MONITOR_STATES}), state)
    assert again == []
    moved = [dict(s) for s in MONITOR_STATES]
    moved[0] = {**moved[0], "attributes": {"automations": [{**FAILED_ROW, "error_step": "action/3"}]}}
    changed, _ = checks.monitors(StatesApi({"/states": moved}), state)
    assert [f["check"] for f in changed] == ["automation_failures"] and "action/3" in changed[0]["detail"]


def test_automation_monitor_with_one_of_its_two_sensors_is_a_failure_to_look():
    for i in range(2):
        states = [s for j, s in enumerate(MONITOR_STATES) if j != i]
        with pytest.raises(RuntimeError, match="no sensor found"):
            checks.monitors(StatesApi({"/states": states}), {})


def test_an_integration_never_seen_is_off_and_one_seen_before_and_gone_is_a_failure_to_look():
    radar_only = [MONITOR_STATES[2]]
    out, state = checks.monitors(StatesApi({"/states": radar_only}), None)
    assert {f["check"] for f in out} == {"breakage_imminent", "breakage_upcoming"} and set(state) == {"near", "later"}
    assert checks.monitors(StatesApi({"/states": []}), None) == ([], {}), "neither installed: silent"
    assert checks.monitors(StatesApi({"/states": []}), {}) == ([], {})
    _, seen = checks.monitors(StatesApi({"/states": MONITOR_STATES}), None)
    with pytest.raises(RuntimeError, match="no sensor found for failed automations, linked entities, breakage radar"):
        checks.monitors(StatesApi({"/states": []}), seen)
    with pytest.raises(RuntimeError, match="no sensor found for breakage radar"):
        checks.monitors(StatesApi({"/states": MONITOR_STATES[:2]}), seen)


def test_run_all_is_silent_about_monitors_that_are_not_installed(supervisor):
    api = checks.Api("test-token", base=supervisor)
    findings, state = checks.run_all(api, options(), {}, NOW)
    assert not [f for f in findings if f["check"] == "monitors"] and state["monitors"] == {}


class FakeMemory(BaseHTTPRequestHandler):
    health: ClassVar[dict] = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        out = json.dumps(FakeMemory.health).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def memory():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMemory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


RAN = {"at": "2026-09-20T03:00:40+00:00", "version": "review-v1", "reviewed": 30, "forgotten": {"duplicate": 9, "session-state": 3}, "kept": 18, "failed": 0, "error": None, "review_at": "03:00"}


def test_memory_review_reports_the_nights_run(memory):
    FakeMemory.health = {"candidates": 18, "candidate_review": RAN}
    [f] = checks.memory_review(memory, NOW)
    assert f["severity"] == "info" and f["title"] == "Memory review: 12 forgotten (9 duplicate, 3 session state), 18 candidates left for agents"


def test_memory_review_warns_when_it_did_not_run_cleanly(memory):
    cases = [
        ({"at": None, "review_at": "03:00"}, "Memory review has not run"),
        ({**RAN, "error": "model server unavailable"}, "Memory review: model server unavailable"),
        ({**RAN, "at": "2026-09-18T03:00:40+00:00"}, "Memory review last ran 48 h ago"),
        ({**RAN, "failed": 2}, "Memory review: 2 candidates failed"),
    ]
    for block, title in cases:
        FakeMemory.health = {"candidates": 18, "candidate_review": block}
        [f] = checks.memory_review(memory, NOW)
        assert f["severity"] == "warn" and f["title"] == title, block


def test_memory_review_is_off_when_empty_and_a_finding_when_unreachable(supervisor):
    assert checks.memory_url({}) == "" and checks.memory_url({"memory_url": ""}) == ""
    assert checks.memory_review("", NOW) == []
    api = checks.Api("test-token", base=supervisor)
    findings, _ = checks.run_all(api, options(memory_url="http://127.0.0.1:9"), {}, NOW)
    [f] = [f for f in findings if f["check"] == "memory_review"]
    assert f["severity"] == "warn" and f["title"].startswith("check memory_review failed")


def test_the_memory_review_line_survives_a_busy_night(supervisor, memory, tmp_path):
    FakeMemory.health = {"candidates": 18, "candidate_review": RAN}
    loops = srv.Loops(options(memory_url=memory), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    run = loops.run_once()
    assert run["counts"]["action"] + run["counts"]["warn"] >= 4, "control: the memory line is past the top four"
    assert "- Memory review: 12 forgotten (9 duplicate, 3 session state), 18 candidates left for agents" in FakeSupervisor.posted[-1][1]["message"]


class FakeCollector(BaseHTTPRequestHandler):
    runs: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        out = json.dumps(FakeCollector.runs).encode()
        self.send_response(200 if self.path == "/runs" else 404)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def collector():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCollector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def crun(started, kind="scheduled", **kw):
    return {
        "id": started,
        "kind": kind,
        "started_at": started,
        "finished_at": started,
        "symbols": 521,
        "written": 521,
        "deduped": 0,
        "failed": 0,
        "not_attempted": 0,
        "aborted": False,
        "bytes": 1,
        "stored_bytes": 1,
        "failed_symbols": [],
        "note": None,
        **kw,
    }


# NOW is Sunday 2026-09-20 03:30 UTC, Saturday evening in New York: the run due is Friday the 18th, 16:20 EDT.
FRIDAY = "2026-09-18T20:20:00Z"


def test_the_due_run_is_the_latest_weekday_whose_window_closed():
    due = checks.collector_due
    utc = dt.UTC
    assert due(NOW) == dt.datetime(2026, 9, 18, 20, 20, tzinfo=utc)
    assert due(dt.datetime(2026, 9, 22, 3, 30, tzinfo=utc)) == dt.datetime(2026, 9, 21, 20, 20, tzinfo=utc), "Tuesday 03:30 UTC is Monday evening"
    assert due(dt.datetime(2026, 9, 21, 20, 30, tzinfo=utc)) == dt.datetime(2026, 9, 18, 20, 20, tzinfo=utc), "a run still inside its window is not yet due"
    assert due(dt.datetime(2026, 9, 21, 23, 20, tzinfo=utc)) == dt.datetime(2026, 9, 21, 20, 20, tzinfo=utc)
    assert due(dt.datetime(2026, 12, 8, 3, 30, tzinfo=utc)) == dt.datetime(2026, 12, 7, 21, 20, tzinfo=utc), "EST runs at 21:20 UTC"


def test_a_good_run_is_info_and_a_holiday_run_of_only_duplicates_is_too(collector):
    FakeCollector.runs = [crun("2026-09-17T20:20:03Z"), crun("2026-09-18T20:20:02Z")]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "info" and f["title"] == "Options collector: 2026-09-18 run ok"
    FakeCollector.runs = [crun("2026-09-17T20:20:03Z"), crun(FRIDAY, written=0, deduped=521)]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "info" and "521 deduped" in f["detail"]


def test_a_missed_day_warns_and_a_manual_run_does_not_count(collector):
    FakeCollector.runs = [crun("2026-09-17T20:20:03Z"), crun("2026-09-18T20:40:00Z", kind="manual")]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "warn" and f["title"] == "Options collector: no run for 2026-09-18"
    FakeCollector.runs = [crun("2026-09-17T20:20:03Z"), crun("2026-09-19T14:00:00Z", kind="catch-up")]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "warn" and "catch-up run, started 2026-09-19T14:00:00Z" in f["detail"]
    FakeCollector.runs = [crun("2026-09-17T20:20:03Z"), crun("2026-09-18T23:30:00Z")]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "warn", "a run started after the window closed is late"


def test_an_aborted_run_and_more_than_five_percent_failed_warn(collector):
    FakeCollector.runs = [crun("2026-09-17T20:20:03Z"), crun(FRIDAY, aborted=True, written=100, failed=3, not_attempted=418, note="stopped on three consecutive 403")]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "warn" and f["title"] == "Options collector: run for 2026-09-18 aborted"
    FakeCollector.runs = [crun(FRIDAY, written=494, failed=27, failed_symbols=["ABC"])]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "warn" and f["title"] == "Options collector: 27 of 521 symbols failed for 2026-09-18"
    FakeCollector.runs = [crun(FRIDAY, written=495, failed=26)]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "info", "26 of 521 is under 5%"


def test_the_failure_share_is_of_the_symbols_attempted(collector):
    FakeCollector.runs = [crun(FRIDAY, symbols=521, written=20, failed=2, not_attempted=499)]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "warn", "2 of 22 attempted is over 5%"


def test_a_day_before_the_collectors_first_run_is_not_due(collector):
    FakeCollector.runs = [crun("2026-09-19T13:17:25Z", kind="manual", symbols=1)]
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "info" and f["title"] == "Options collector: nothing due for 2026-09-18 (collecting since 2026-09-19 13:17 UTC)"
    FakeCollector.runs = []
    [f] = checks.options_collector(collector, NOW)
    assert f["severity"] == "info" and "no run yet" in f["title"]


def test_an_unreadable_collector_is_could_not_look_and_empty_turns_it_off(supervisor):
    assert checks.collector_url({}) == "" and checks.collector_url({"collector_url": ""}) == ""
    assert checks.options_collector("", NOW) == []
    [f] = checks.options_collector("http://127.0.0.1:9", NOW)
    assert f["severity"] == "warn" and f["title"].startswith("Options collector: could not look")
    api = checks.Api("test-token", base=supervisor)
    findings, _ = checks.run_all(api, options(collector_url="http://127.0.0.1:9"), {}, NOW)
    assert [f["severity"] for f in findings if f["check"] == "options_collector"] == ["warn"]


def test_the_collector_line_survives_a_busy_night(supervisor, memory, collector, tmp_path):
    FakeMemory.health = {"candidates": 18, "candidate_review": RAN}
    FakeCollector.runs = [crun(FRIDAY, aborted=True, note="stopped")]
    loops = srv.Loops(options(memory_url=memory, collector_url=collector), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    loops.run_once()
    assert "- Options collector: run for 2026-09-18 aborted" in FakeSupervisor.posted[-1][1]["message"]


class StatsSocket:
    """Core's websocket for the HVAC statistics: cooling days at 85 F, 0.5 kWh an hour per circuit. NOW is 19 Sep
    local, so the 17th and 18th are the complete days."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, kind, **kw):
        import controller as ctl
        from zoneinfo import ZoneInfo

        assert kind == "recorder/statistics_during_period"
        tz = ZoneInfo("America/Chicago")
        hours = [h for d in range(17, 20) for h in ctl.expected_hours(dt.date(2026, 9, d), tz)]
        key = kw["types"][0]
        return {sid: [{"start": h * 1000, key: 85.0 if key == "mean" else 0.5} for h in hours] for sid in kw["statistic_ids"]}


def test_the_nightly_run_measures_hvac_and_scores_the_action_record(supervisor, tmp_path):
    hvac = {"hvac_zones": ["central=sensor.ac,sensor.fu"], "outdoor_temperature": "sensor.out", "controller_from": "2026-09-17"}
    loops = srv.Loops(options(**hvac), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    loops.socket = StatsSocket
    run = loops.run_once()
    hvac_rows = [f for f in run["findings"] if f["check"] == "hvac_per_degree_day"]
    assert hvac_rows and hvac_rows[0]["severity"] == "info" and "2 counting days, 5 needed" in hvac_rows[0]["title"]
    assert len(loops.controller.days()) == 2 and loops.controller.days()[0]["cdd65"] == 20.0
    actions = [f for f in run["findings"] if f["check"] == "manual_actions"]
    assert actions and actions[0]["title"].startswith("manual actions: 0 complete days of 60") and actions[0]["title"].endswith("(provisional)")
    unset = srv.Loops(options(), str(tmp_path / "x"), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    unset.socket = StatsSocket
    assert unset.loop7(NOW) == [], "no zones: loop 7 is off and says nothing"
    half = srv.Loops(options(hvac_zones=hvac["hvac_zones"]), str(tmp_path / "y"), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    half.socket = StatsSocket
    assert half.loop7(NOW)[0]["title"] == "loop 7 not measuring: hvac_zones and outdoor_temperature are not set"


def test_only_the_supervisor_core_and_named_add_ons_reach_the_api(supervisor, tmp_path):
    loops = srv.Loops(options(notify_target=""), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    http = srv.serve(loops, 0, host="127.0.0.1")  # the real peers: 127.0.0.1 is none of them
    base = f"http://127.0.0.1:{http.server_address[1]}"

    def status(path, data=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET"), timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    try:
        assert status("/healthz") == 200
        for path in ("/", "/api/latest.json", "/api/snapshot.json", "/api/network.json", "/api/overrides.json"):
            assert status(path) == 403, path
        assert status("/api/run", b"{}") == 403
        assert loops.store.history() == [], "a refused run never ran"
        loops.options = {**loops.options, "api_peers": ["localhost"]}
        assert status("/api/latest.json") == 200
        loops.options = {**loops.options, "api_peers": []}
        assert status("/api/latest.json") == 403
    finally:
        http.shutdown()


def test_a_peer_name_is_resolved_at_each_request():
    seen = []

    def resolve(name):
        seen.append(name)
        if name == "gone":
            raise OSError("no such host")
        return name, [], ["172.30.33.7"]

    assert srv.peer_allowed("172.30.32.2", srv.FIXED_PEERS, [], resolve)
    assert srv.peer_allowed("172.30.33.7", srv.FIXED_PEERS, ["gone", "a0d7b954-ssh"], resolve)
    assert not srv.peer_allowed("172.30.33.8", srv.FIXED_PEERS, ["a0d7b954-ssh"], resolve)
    assert seen == ["gone", "a0d7b954-ssh", "a0d7b954-ssh"]
