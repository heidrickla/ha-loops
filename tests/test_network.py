"""The network checks against a fake UniFi OS controller."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import checks
import network as nw
import server as srv
from test_loops import NOW, FakeSupervisor, options, supervisor  # noqa: F401

SITE = "/proxy/network/api/s/default"
V2 = "/proxy/network/v2/api/site/default"


def pol(pid, index, name, action, src, dst, predefined=False, **kw):
    return {"_id": pid, "index": index, "name": name, "action": action, "enabled": True, "predefined": predefined, "protocol": "all", "source": src, "destination": dst, **kw}


NETWORKS = [
    {"_id": "w1", "name": "Fiber Internet", "purpose": "wan", "wan_networkgroup": "WAN"},
    {"_id": "w2", "name": "Cable Internet", "purpose": "wan", "wan_networkgroup": "WAN2"},
    {"_id": "w4", "name": "Cellular Backup Internet", "purpose": "wan", "wan_networkgroup": "WAN4"},
    {"_id": "n52", "name": "IoT", "purpose": "corporate", "vlan": 52},
    {"_id": "n53", "name": "Cameras", "purpose": "corporate", "vlan": 53, "network_isolation_enabled": True},
    {"_id": "n54", "name": "LAN", "purpose": "corporate", "vlan": 54},
    {"_id": "n50", "name": "Management", "purpose": "corporate"},
    {"_id": "n254", "name": "Guest", "purpose": "corporate", "vlan": 254},
]
ZONES = [
    {"_id": "zi", "name": "Internal", "zone_key": "internal", "network_ids": ["n52", "n53", "n54", "n50", "n254"]},
    {"_id": "ze", "name": "External", "zone_key": "external", "network_ids": []},
    {"_id": "zg", "name": "Gateway", "zone_key": "gateway", "network_ids": []},
]
INTERNAL_ANY = {"zone_id": "zi", "matching_target": "ANY"}
RDP = {"zone_id": "zi", "matching_target": "IP", "ips": ["10.20.54.30"], "port": "3389", "port_matching_type": "SPECIFIC"}
POLICIES = [
    # narrower than "IoT to all of internal": an exception, not the decider
    pol(
        "p10000",
        10000,
        "Allow IoT to HA",
        "ALLOW",
        {"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n52"]},
        {"zone_id": "zi", "matching_target": "IP", "ips": ["10.20.52.10"], "port": "8123", "port_matching_type": "SPECIFIC"},
        protocol="tcp",
    ),
    pol("p10001", 10001, "Block Guest to Internal", "BLOCK", {"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n254"]}, INTERNAL_ANY),
    pol("p10002", 10002, "Phone-MAC-Any", "ALLOW", {"zone_id": "ze", "matching_target": "MAC", "mac_addresses": ["aa:aa:aa:aa:aa:aa"]}, INTERNAL_ANY),
    pol("p10003", 10003, "RDP Everywhere", "ALLOW", RDP, INTERNAL_ANY, protocol="tcp"),
    pol("p10004", 10004, "Allow RDP to Desk", "ALLOW", {"zone_id": "zi", "matching_target": "IP", "ips": ["10.20.54.20"]}, RDP, protocol="tcp"),
    pol("p10005", 10005, "Allow RDP to Desk", "ALLOW", {"zone_id": "zi", "matching_target": "IP", "ips": ["10.20.54.20"]}, RDP, protocol="tcp"),
    pol("p1", 1, "Allow Return Traffic", "ALLOW", {"zone_id": "ze", "matching_target": "ANY"}, INTERNAL_ANY, predefined=True, connection_state_type="RESPOND_ONLY"),
    pol("p30000", 30000, "Allow All Traffic", "ALLOW", INTERNAL_ANY, INTERNAL_ANY, predefined=True),
]
DOCS = {
    SITE + "/rest/networkconf": NETWORKS,
    SITE + "/stat/health": [
        {"subsystem": "wan", "status": "ok", "uptime_stats": {"WAN": {"availability": 100.0, "latency_average": 10}, "WAN2": {"availability": 97.5, "latency_average": 14}}},
        {"subsystem": "www", "status": "ok"},
    ],
    SITE + "/stat/device": [
        {"type": "uap", "name": "AP"},
        {"type": "udm", "name": "UDM", "wan1": {"up": True}, "wan2": {"up": True}, "wan4": {"up": False}, "last_wan_status": {"WAN": "online", "WAN2": "online", "WAN4": "offline"}},
    ],
    SITE + "/stat/sta": [
        {"mac": "AA:BB:CC:00:00:01", "network_id": "n50", "network": "Management"},
        {"mac": "aa:bb:cc:00:00:02", "network_id": "n53", "network": "Cameras"},
        {"mac": "11:22:33:44:55:66", "network_id": "n54", "network": "LAN"},
    ],
    SITE + "/rest/portforward": [
        {"_id": "f1", "name": "web", "enabled": True, "proto": "tcp", "dst_port": "443", "fwd": "10.20.54.20", "fwd_port": "8443", "src": "any"},
        {"_id": "f2", "name": "old", "enabled": False, "proto": "tcp", "dst_port": "80"},
    ],
    SITE + "/rest/setting": [{"key": "mgmt"}, {"key": "usg", "upnp_enabled": True}],
    V2 + "/firewall-policies": POLICIES,
    V2 + "/firewall/zone-matrix": ZONES,
}
CAMERAS = {"aa:bb:cc:00:00:01": "Front Door", "aa:bb:cc:00:00:02": "Garage", "aa:bb:cc:00:00:03": "Shed"}
FIRST = {
    "wan_down:WAN4",
    "wan_degraded:WAN2",
    "isolation:IoT",
    "camera_network:aa:bb:cc:00:00:01",
    "external_mac:p10002",
    "source_port:p10003",
    "duplicate_policy:p10005",
    "dns_egress",
    "port_forward:f1",
    "upnp",
}


class FakeUnifi(BaseHTTPRequestHandler):
    docs: ClassVar[dict] = {}
    fail: ClassVar[set] = set()
    logouts: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def _send(self, code: int, doc=None, headers=()):
        body = json.dumps(doc).encode() if doc is not None else b""
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/auth/login":
            if json.loads(body) == {"username": "ro", "password": "pw"}:
                self._send(200, {}, [("Set-Cookie", "TOKEN=abc; Path=/"), ("X-CSRF-Token", "csrf1")])
            else:
                self._send(401, {"error": "bad"})
        elif self.path == "/api/auth/logout":
            FakeUnifi.logouts.append(self.headers.get("X-CSRF-Token"))
            self._send(200, {})
        else:
            self._send(404)

    def do_GET(self):
        if "TOKEN=abc" not in (self.headers.get("Cookie") or ""):
            self._send(401)
        elif self.path in FakeUnifi.fail or self.path not in FakeUnifi.docs:
            self._send(500 if self.path in FakeUnifi.fail else 404)
        elif self.path.startswith(V2):
            self._send(200, FakeUnifi.docs[self.path])
        else:
            self._send(200, {"meta": {"rc": "ok"}, "data": FakeUnifi.docs[self.path]})


@pytest.fixture
def unifi():
    FakeUnifi.docs = copy.deepcopy(DOCS)
    FakeUnifi.fail = set()
    FakeUnifi.logouts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUnifi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def opts(url, **extra):
    return {"unifi_url": url, "unifi_username": "ro", "unifi_password": "pw", **extra}


def reported(findings) -> set[str]:
    return {k for f in findings if f["check"].startswith("network_") for k in f["value"]}


def test_the_first_run_reports_every_rule_and_the_second_nothing(unifi):
    findings, state = nw.check(opts(unifi), None, lambda: CAMERAS)
    assert reported(findings) == FIRST
    assert all("first run" in f["title"] for f in findings)
    assert {f["check"]: f["severity"] for f in findings}["network_wan_down"] == "action"
    assert "[dismiss: wan_down:WAN4]" in next(f["detail"] for f in findings if f["check"] == "network_wan_down")
    assert set(state["items"]) == FIRST and FakeUnifi.logouts == ["csrf1"]
    again, _ = nw.check(opts(unifi), state, lambda: CAMERAS)
    assert again == []


def test_isolation_follows_the_first_broad_policy():
    data = {"networks": NETWORKS, "zones": ZONES, "policies": POLICIES}
    items, problems = nw.isolation_items(data, ["IoT", "Cameras", "Guest", "Lab"])
    assert problems == []
    assert items == {
        "isolation:IoT": {"text": "IoT reaches the internal zone through 'Allow All Traffic'", "value": "allowed by Allow All Traffic"},
        "isolation:Lab": {"text": "no network named Lab", "value": "missing"},
    }
    # a broad user ALLOW before the block decides for Guest
    allow = pol("p9999", 9999, "Guest anywhere", "ALLOW", {"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n254"]}, INTERNAL_ANY)
    items, _ = nw.isolation_items({**data, "policies": [allow, *POLICIES]}, ["Guest"])
    assert items["isolation:Guest"]["value"] == "allowed by Guest anywhere"
    # a block to every private range counts as a block to the internal zone
    block = pol(
        "p9998",
        9998,
        "IoT no private",
        "BLOCK",
        {"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n52"]},
        {"zone_id": "zi", "matching_target": "IP", "ips": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]},
    )
    items, _ = nw.isolation_items({**data, "policies": [block, *POLICIES]}, ["IoT"])
    assert items == {}


def test_a_new_item_is_reported_alone_and_a_dismissed_one_never(unifi):
    _, state = nw.check(opts(unifi, network_dismiss=["upnp"]), None, lambda: CAMERAS)
    assert "upnp" not in state["items"] and state["dismissed"] == ["upnp"]
    FakeUnifi.docs[SITE + "/rest/portforward"].append({"_id": "f3", "name": "ssh", "enabled": True, "proto": "tcp", "dst_port": "22", "fwd": "10.20.54.21", "fwd_port": "22"})
    findings, state = nw.check(opts(unifi, network_dismiss=["upnp"]), state, lambda: CAMERAS)
    [f] = findings
    assert f["check"] == "network_port_forward" and f["value"] == ["port_forward:f3"] and f["title"] == "1 new port forwards"
    # undismissed, it is new
    findings, _ = nw.check(opts(unifi), state, lambda: CAMERAS)
    assert reported(findings) == {"upnp"}


def test_a_changed_value_is_reported_again(unifi):
    _, state = nw.check(opts(unifi), None, lambda: CAMERAS)
    FakeUnifi.docs[SITE + "/stat/sta"][0].update(network_id="n54", network="LAN")
    findings, _ = nw.check(opts(unifi), state, lambda: CAMERAS)
    [f] = findings
    assert f["value"] == ["camera_network:aa:bb:cc:00:00:01"] and "Front Door (aa:bb:cc:00:00:01) on LAN" in f["detail"]


def test_an_unreadable_source_warns_every_run_and_keeps_its_items(unifi):
    _, state = nw.check(opts(unifi), None, lambda: CAMERAS)
    FakeUnifi.fail = {V2 + "/firewall/zone-matrix", V2 + "/firewall/zones"}
    for _ in range(2):
        findings, state = nw.check(opts(unifi), state, lambda: CAMERAS)
        [f] = findings
        assert f["check"] == "network" and f["severity"] == "warn" and f["detail"].startswith("zones: HTTPError")
        assert {"isolation:IoT", "dns_egress", "duplicate_policy:p10005"} <= set(state["items"])
    FakeUnifi.fail = set()
    findings, _ = nw.check(opts(unifi), state, lambda: CAMERAS)
    assert findings == []


def test_the_zone_list_falls_back_to_zones(unifi):
    FakeUnifi.docs[V2 + "/firewall/zones"] = FakeUnifi.docs.pop(V2 + "/firewall/zone-matrix")
    findings, _ = nw.check(opts(unifi), None, lambda: CAMERAS)
    assert reported(findings) == FIRST


def test_off_unconfigured_and_a_refused_login():
    assert nw.check({"unifi_url": ""}, None) == ([], None)
    [f], state = nw.check({"unifi_url": "http://127.0.0.1:9"}, None)
    assert f["severity"] == "warn" and f["title"] == "network checks: no UniFi account in the options" and state is None
    [f], state = nw.check({"unifi_username": "ro"}, None)
    assert f["title"] == "network checks: no UniFi account in the options", "absent url means the default, which still needs the account"


def test_a_refused_login_is_a_finding_and_keeps_the_state(unifi):
    [f], state = nw.check(opts(unifi, unifi_password="wrong"), None)
    assert f["title"] == "network checks: UniFi login failed: HTTPError: HTTP Error 401: Unauthorized" and state is None


def test_a_latency_step_needs_five_nights_and_twice_the_median():
    w = {"wan": "WAN", "label": "Fiber", "status": "online", "up": True, "availability": 100.0, "latency": 35.0, "read": True}
    items, history, _ = nw.wan_items([w], {"WAN": [10.0, 11.0, 10.0, 12.0]})
    assert items == {} and history["WAN"] == [10.0, 11.0, 10.0, 12.0, 35.0]
    items, _, _ = nw.wan_items([w], {"WAN": [10.0, 11.0, 10.0, 12.0, 10.0]})
    assert items == {"wan_latency:WAN": {"text": "WAN (Fiber) 35 ms against a 5-night median of 10 ms", "value": "step"}}
    items, _, _ = nw.wan_items([{**w, "latency": 25.0}], {"WAN": [10.0] * 5})
    assert items == {}, "twice the median but under median + 20 ms"
    _, history, _ = nw.wan_items([w], {"WAN": [10.0] * 14})
    assert len(history["WAN"]) == 14 and history["WAN"][-1] == 35.0


def test_an_unread_wan_is_named_and_keeps_its_history():
    w = {"wan": "WAN3", "label": "DSL", "status": None, "up": None, "availability": None, "latency": None, "read": False}
    items, history, unread = nw.wan_items([w], {"WAN3": [9.0]})
    assert items == {} and history == {"WAN3": [9.0]} and unread == ["WAN3"]


def test_ports_and_broad():
    assert nw.ports({"port": "53,853,1000-1002"}) == {53, 853, 1000, 1001, 1002}
    assert nw.any_port({}) and not nw.any_port({"port": "53"}) and not nw.any_port({"port_matching_type": "OBJECT", "port_group_id": "g1"})
    assert nw.broad(POLICIES[7]) and not nw.broad(POLICIES[6]) and not nw.broad(POLICIES[0])
    assert not nw.broad({**POLICIES[7], "schedule": {"mode": "EVERY_DAY"}})


def test_a_dns_block_to_the_internet_clears_dns_egress():
    block = pol("p10010", 10010, "No outside DNS", "BLOCK", INTERNAL_ANY, {"zone_id": "ze", "matching_target": "ANY", "port": "53,853", "port_matching_type": "SPECIFIC"}, protocol="tcp_udp")
    data = {"zones": ZONES, "policies": [*POLICIES, block]}
    assert "dns_egress" not in nw.policy_items(data) and "dns_egress" in nw.policy_items({**data, "policies": POLICIES})


def test_protect_macs_come_from_the_device_registry():
    class Ws:
        def call(self, kind):
            assert kind == "config/device_registry/list"
            return [
                {"name": "Front Door", "identifiers": [["unifiprotect", "AABBCC000001"]], "connections": [["mac", "AA:BB:CC:00:00:01"]]},
                {"name": "Lamp", "name_by_user": None, "identifiers": [["zha", "x"]], "connections": [["mac", "11:22:33:44:55:66"]]},
                {"name": "Sensor", "identifiers": [["unifiprotect", "s1"]], "connections": []},
            ]

    assert nw.protect_macs(Ws()) == {"aa:bb:cc:00:00:01": "Front Door"}


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def call(self, kind, **fields):
        if kind == "config/device_registry/list":
            return [{"name": "Front Door", "identifiers": [["unifiprotect", "a"]], "connections": [["mac", "aa:bb:cc:00:00:01"]]}]
        raise RuntimeError(kind)


def test_the_nightly_run_reports_network_items_and_serves_them(supervisor, unifi, tmp_path):  # noqa: F811
    loops = srv.Loops(options(**opts(unifi)), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    loops.socket = FakeSocket
    run = loops.run_once()
    assert reported(run["findings"]) == FIRST
    message = FakeSupervisor.posted[-1][1]["message"]
    assert run["counts"]["action"] + run["counts"]["warn"] > 4, "control: network lines are past the top four"
    assert all(f"- {f['title']}" in message for f in run["findings"] if f["check"].startswith("network"))
    http = srv.serve(loops, 0, host="127.0.0.1")
    try:
        doc = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{http.server_address[1]}/api/network.json").read())
    finally:
        http.shutdown()
    assert set(doc["items"]) == FIRST and doc["wans"][0]["wan"] == "WAN" and "uptime_stats" in doc["shape"]["health_wan"]
    later = loops.now() + dt.timedelta(days=1)
    loops.now = lambda: later
    assert not [f for f in loops.run_once()["findings"] if f["check"].startswith("network")]
