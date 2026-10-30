"""A fresh install: no option typed, the checks that need a service of their own stay silent, values
that can be found are found, and the add-on asks the Supervisor for no more than its role allows."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import checks
import server as srv
from test_loops import NOW, FakeSupervisor, StatesApi, options, supervisor  # noqa: F401
from test_loops import StatsSocket as _StatsSocket

ROOT = Path(__file__).resolve().parent.parent
GENERIC = {"core_version", "addon_updates", "supervisor_issues", "backup_age", "disk", "unavailable_entities", "safety_devices", "battery_low", "pending_updates", "core_log_errors", "manual_actions"}

# Supervisor API paths an add-on with `hassio_role: homeassistant` may call (supervisor/api/middleware/
# security.py, the v1 ROLE_HOMEASSISTANT pattern and the paths with no role check, read 2026-10-30).
HOMEASSISTANT_ROLE = re.compile(r"^(?:/.+/info|/core/.+|/homeassistant/.+|/supervisor/ping)$")


def test_a_fresh_install_reports_only_the_generic_checks(supervisor, tmp_path):  # noqa: F811
    loops = srv.Loops({"run_at": "03:30", "notify_target": ""}, str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    run = loops.run_once()
    assert {f["check"] for f in run["findings"]} <= GENERIC, run["findings"]
    assert set(run["optional"].values()) == {"off"} and len(run["optional"]) == 5
    assert FakeSupervisor.posted == [], "no notify target, nothing sent"


def test_the_uptime_kuma_add_on_is_found_when_the_key_is_set():
    kuma = [{"name": "Vaultwarden", "slug": "a0d7b954_bitwarden"}, {"name": "Uptime Kuma", "slug": "a0d7b954_uptime-kuma"}]
    assert checks.kuma_url({}, kuma) == "http://a0d7b954-uptime-kuma:3001"
    assert checks.kuma_url({"kuma_url": "http://kuma.example:3001"}, kuma) == "http://kuma.example:3001"
    assert checks.kuma_url({}, kuma[:1]) == ""


def test_a_kuma_key_without_a_kuma_says_what_is_missing_and_no_key_is_silent(supervisor):  # noqa: F811
    api = checks.Api("test-token", base=supervisor)
    findings, state = checks.run_all(api, options(kuma_api_key="k"), {}, NOW)
    [f] = [f for f in findings if f["check"] == "kuma_down"]
    assert f["severity"] == "warn" and "kuma_url is empty and no Uptime Kuma add-on is installed" in f["title"]
    assert state["optional"]["uptime_kuma"] == "kuma_api_key set, no kuma_url"
    findings, state = checks.run_all(api, options(kuma_url="http://127.0.0.1:9"), {}, NOW)
    assert not [f for f in findings if f["check"] == "kuma_down"] and state["optional"]["uptime_kuma"] == "off"


class StatsSocket(_StatsSocket):
    def call(self, kind, **kw):
        if kind in ("config_entries/get", "config/device_registry/list"):
            return []
        return super().call(kind, **kw)


def test_every_supervisor_path_read_is_one_the_homeassistant_role_allows(supervisor, tmp_path):  # noqa: F811
    assert not HOMEASSISTANT_ROLE.match("/addons") and not HOMEASSISTANT_ROLE.match("/backups"), "control: the manager-only lists"
    everything = options(
        kuma_api_key="k",
        unifi_username="ro",
        unifi_password="pw",
        camera_network="Cameras",
        hvac_zones=["central=sensor.ac"],
        outdoor_temperature="sensor.out",
        controller_from="2026-09-17",
    )
    loops = srv.Loops(everything, str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    loops.socket = StatsSocket
    run = loops.run_once()
    paths = {p for p in FakeSupervisor.paths if not p.startswith("/core/api/")}
    assert {"/supervisor/info", "/backups/info", "/network/info", "/core/logs"} <= paths, paths
    assert all(HOMEASSISTANT_ROLE.match(p) for p in paths), sorted(p for p in paths if not HOMEASSISTANT_ROLE.match(p))
    assert run["optional"]["unifi_network"] == "on" and run["optional"]["hvac_per_degree_day"] == "on"
    config = (ROOT / "loops" / "config.yaml").read_text(encoding="utf-8")
    assert "\nhassio_role: homeassistant\n" in config


def test_the_version_is_the_same_everywhere():
    config = re.search(r'^version: "([^"]+)"$', (ROOT / "loops" / "config.yaml").read_text(encoding="utf-8"), re.M)[1]
    project = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)[1]
    changelog = re.search(r"^## (\S+)$", (ROOT / "loops" / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)[1]
    assert config == project == changelog == srv.VERSION


def test_the_form_shows_two_options_and_every_option_is_described():
    text = (ROOT / "loops" / "config.yaml").read_text(encoding="utf-8")
    shown = re.findall(r"^  (\w+):", text.split("\noptions:\n", 1)[1].split("\nschema:\n", 1)[0], re.M)
    schema = re.findall(r"^  (\w+):", text.split("\nschema:\n", 1)[1], re.M)
    described = re.findall(r"^  (\w+):$", (ROOT / "loops" / "translations" / "en.yaml").read_text(encoding="utf-8"), re.M)
    assert shown == ["run_at", "notify_target"]
    assert set(schema) == set(described) and len(schema) == 26


def test_no_value_from_one_home_is_shipped():
    # Built from parts, so this file does not match itself; the home's own addresses and host names
    # come from HA_DEV_HOST_NAMES, since naming them in a published file is the disclosure this guards.
    banned = ["homeassistant" + ".local", *(n.strip().lower() for n in os.environ.get("HA_DEV_HOST_NAMES", "").split(",") if n.strip())]
    files = [p for p in [*ROOT.glob("loops/**/*"), *ROOT.glob("tests/*.py"), ROOT / "README.md", ROOT / "repository.yaml"] if p.is_file() and p.name != Path(__file__).name]
    hits = [(p.relative_to(ROOT).as_posix(), b) for p in files for b in banned if b in p.read_text(encoding="utf-8", errors="replace").lower()]
    assert hits == []


def test_device_only_automations_are_looked_at_not_a_failure():
    states = [{"entity_id": "automation.porch", "state": "on", "attributes": {"id": "1"}}]
    docs = {
        "/states": states,
        "/config/automation/config/1": {
            "triggers": [{"device_id": "abc", "domain": "zha", "type": "remote_button_short_press"}],
            "actions": [{"device_id": "def", "domain": "light", "type": "turn_on"}],
        },
    }
    assert checks.broken_refs(StatesApi(docs), None) == ([], [])
