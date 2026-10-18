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
    return {"_id": pid, "index": index, "name": name, "action": action, "enabled": True, "predefined": predefined, "protocol": "all", "source": src, "destination": dst, "hits": 0, **kw}


NETWORKS = [
    {"_id": "w1", "name": "Fiber Internet", "purpose": "wan", "wan_networkgroup": "WAN"},
    {"_id": "w2", "name": "Cable Internet", "purpose": "wan", "wan_networkgroup": "WAN2"},
    {"_id": "w3", "name": "DSL Internet", "purpose": "wan", "wan_networkgroup": "WAN3"},
    {"_id": "w4", "name": "Cellular Backup Internet", "purpose": "wan", "wan_networkgroup": "WAN4"},
    {"_id": "n52", "name": "IoT", "purpose": "corporate", "vlan": 52, "ip_subnet": "10.20.52.1/24"},
    {"_id": "n53", "name": "Cameras", "purpose": "corporate", "vlan": 53, "ip_subnet": "10.20.53.1/24", "network_isolation_enabled": True},
    {"_id": "n54", "name": "LAN", "purpose": "corporate", "vlan": 54, "ip_subnet": "10.20.54.1/24"},
    {"_id": "n50", "name": "Management", "purpose": "corporate", "ip_subnet": "10.30.50.1/24"},
    {"_id": "n254", "name": "Guest", "purpose": "corporate", "vlan": 254, "ip_subnet": "10.20.254.1/24"},
]
ZONES = [
    {"_id": "zi", "name": "Internal", "zone_key": "internal", "network_ids": ["n52", "n53", "n54", "n50", "n254"]},
    {"_id": "ze", "name": "External", "zone_key": "external", "network_ids": []},
    {"_id": "zg", "name": "Gateway", "zone_key": "gateway", "network_ids": []},
]
# the matrix's zones carry no network list
MATRIX = [{"_id": z["_id"], "name": z["name"], "zone_key": z["zone_key"], "data": [{"zone_id": "zi", "action": "ALLOW"}]} for z in ZONES]
INTERNAL_ANY = {"zone_id": "zi", "matching_target": "ANY", "port_matching_type": "ANY"}
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
    pol("p10002", 10002, "Phone-MAC-Any", "ALLOW", {"zone_id": "ze", "matching_target": "MAC", "macs": ["aa:aa:aa:aa:aa:aa"]}, INTERNAL_ANY),
    # the same rule for a second zone pair, numbered within that pair
    pol("p10002b", 10000, "Phone-MAC-Any", "ALLOW", {"zone_id": "ze", "matching_target": "MAC", "macs": ["aa:aa:aa:aa:aa:aa"]}, {"zone_id": "zg", "matching_target": "ANY"}),
    pol("p10003", 10003, "RDP Everywhere", "ALLOW", RDP, INTERNAL_ANY, protocol="tcp"),
    pol("p10004", 10004, "Allow RDP to Desk", "ALLOW", {"zone_id": "zi", "matching_target": "IP", "ips": ["10.20.54.20"]}, RDP, protocol="tcp", hits=12),
    # a copy: only its id, index and use counters differ
    pol("p10005", 10005, "Allow RDP to Desk", "ALLOW", {"zone_id": "zi", "matching_target": "IP", "ips": ["10.20.54.20"]}, RDP, protocol="tcp", hits=3, last_hit=1760000000),
    # the same name, made separately, matching something else
    pol("p10006", 10006, "Allow RDP to Desk", "ALLOW", {"zone_id": "zi", "matching_target": "CLIENT", "client_macs": ["11:22:33:44:55:66"]}, RDP, protocol="tcp"),
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
    V2 + "/firewall/zone": ZONES,
    V2 + "/firewall/zone-matrix": MATRIX,
}
CAMERAS = {"aa:bb:cc:00:00:01": "Front Door", "aa:bb:cc:00:00:02": "Garage", "aa:bb:cc:00:00:03": "Shed"}
FIRST = {
    "wan_down:WAN3",
    "wan_down:WAN4",
    "wan_degraded:WAN2",
    "isolation:IoT",
    "camera_network:aa:bb:cc:00:00:01",
    "external_mac:p10002",
    "external_mac:p10002b",
    "same_name:p10006",
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


def run(url, state=None, **extra):
    return nw.check(opts(url, **extra), state, lambda: CAMERAS)


def test_the_first_run_reports_every_rule_and_the_second_nothing(unifi):
    findings, state = run(unifi)
    assert reported(findings) == FIRST
    assert all("first run" in f["title"] for f in findings if f["check"] != "network")
    assert {f["check"]: f["severity"] for f in findings}["network_wan_down"] == "action"
    assert "WAN3 (DSL Internet): no state on the gateway [dismiss: wan_down:WAN3]" in next(f["detail"] for f in findings if f["check"] == "network_wan_down")
    assert set(state["items"]) == FIRST and FakeUnifi.logouts == ["csrf1"]
    assert state["shape"]["paths"]["zones"] == V2 + "/firewall/zone"
    again, _ = run(unifi, state)
    assert [f for f in again if f["check"] != "network"] == []


def test_isolation_follows_the_first_broad_policy():
    data = {"networks": NETWORKS, "zones": ZONES, "policies": POLICIES}
    items, undecided, problems = nw.isolation_items(data, ["IoT", "Cameras", "Guest", "Lab"])
    assert problems == [] and undecided == set()
    assert items == {
        "isolation:IoT": {"text": "IoT reaches the internal zone through 'Allow All Traffic'", "value": "allowed by p30000"},
        "isolation:Lab": {"text": "no network named Lab", "value": "missing"},
    }
    # a broad user ALLOW before the block decides for Guest
    allow = pol("p9999", 9999, "Guest anywhere", "ALLOW", {"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n254"]}, INTERNAL_ANY)
    items, _, _ = nw.isolation_items({**data, "policies": [allow, *POLICIES]}, ["Guest"])
    assert items["isolation:Guest"]["value"] == "allowed by p9999"


def test_an_ip_block_counts_when_it_covers_the_other_internal_subnets():
    def block(ips):
        return pol("p9998", 9998, "IoT no internal", "BLOCK", {"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n52"]}, {"zone_id": "zi", "matching_target": "IP", "ips": ips})

    data = {"networks": NETWORKS, "zones": ZONES}
    for ips, isolated in ((["10.0.0.0/8"], True), (["10.20.0.0/16", "10.30.50.0/24"], True), (["10.20.0.0/16"], False), (["192.168.0.0/16"], False)):
        items, _, _ = nw.isolation_items({**data, "policies": [block(ips), *POLICIES]}, ["IoT"])
        assert (items == {}) is isolated, ips


def test_zone_membership_comes_from_the_zone_list_or_the_network():
    # the matrix carries no network list; the networks name their zone
    nets = [{**n, "firewall_zone_id": "zi"} if n.get("purpose") == "corporate" else n for n in NETWORKS]
    items, undecided, _ = nw.isolation_items({"networks": nets, "zones": MATRIX, "policies": POLICIES}, ["IoT"])
    assert set(items) == {"isolation:IoT"} and undecided == set()
    items, undecided, problems = nw.isolation_items({"networks": NETWORKS, "zones": MATRIX, "policies": POLICIES}, ["IoT"])
    assert items == {} and undecided == {"isolation"} and problems == ["the zone list names no network's zone"]


def test_the_zone_list_falls_back_to_the_matrix(unifi):
    del FakeUnifi.docs[V2 + "/firewall/zone"]
    FakeUnifi.docs[SITE + "/rest/networkconf"] = [{**n, "firewall_zone_id": "zi"} if n.get("purpose") == "corporate" else n for n in NETWORKS]
    findings, state = run(unifi)
    assert reported(findings) == FIRST and state["shape"]["paths"]["zones"] == V2 + "/firewall/zone-matrix"


def test_a_new_item_is_reported_alone_and_a_dismissed_one_never(unifi):
    _, state = run(unifi, network_dismiss=["upnp"])
    assert "upnp" not in state["items"] and state["dismissed"] == ["upnp"]
    FakeUnifi.docs[SITE + "/rest/portforward"].append({"_id": "f3", "name": "ssh", "enabled": True, "proto": "tcp", "dst_port": "22", "fwd": "10.20.54.21", "fwd_port": "22"})
    findings, state = run(unifi, state, network_dismiss=["upnp"])
    [f] = [f for f in findings if f["check"] != "network"]
    assert f["check"] == "network_port_forward" and f["value"] == ["port_forward:f3"] and f["title"] == "1 new port forwards"
    # undismissed, it is new
    findings, _ = run(unifi, state)
    assert reported(findings) == {"upnp"}


def test_a_changed_value_is_reported_again(unifi):
    _, state = run(unifi)
    FakeUnifi.docs[SITE + "/stat/sta"][0].update(network_id="n54", network="LAN")
    findings, _ = run(unifi, state)
    assert reported(findings) == {"camera_network:aa:bb:cc:00:00:01"}
    assert "Front Door (aa:bb:cc:00:00:01) on LAN" in next(f["detail"] for f in findings if f["check"] == "network_camera_network")


def test_a_policy_changed_under_its_id_and_name_is_reported_again(unifi):
    _, state = run(unifi)
    [p] = [p for p in FakeUnifi.docs[V2 + "/firewall-policies"] if p["_id"] == "p10003"]
    p["source"] = {**p["source"], "port": "22"}
    findings, _ = run(unifi, state)
    assert reported(findings) == {"source_port:p10003"}


def test_use_counters_do_not_change_a_policy():
    a, b = (next(p for p in POLICIES if p["_id"] == pid) for pid in ("p10004", "p10005"))
    assert nw.digest(a) == nw.digest(b) and nw.digest({**a, "hits": 99}) == nw.digest(a)
    assert nw.digest({**a, "action": "BLOCK"}) != nw.digest(a)


def test_an_unreadable_source_warns_every_run_and_keeps_its_items(unifi):
    _, state = run(unifi)
    FakeUnifi.fail = {V2 + "/firewall/zone", V2 + "/firewall/zones", V2 + "/firewall/zone-matrix"}
    for _ in range(2):
        findings, state = run(unifi, state)
        [f] = findings
        assert f["check"] == "network" and f["severity"] == "warn" and f["detail"].startswith("zones: HTTPError")
        assert set(state["items"]) >= FIRST
    FakeUnifi.fail = set()
    findings, _ = run(unifi, state)
    assert findings == []


def test_a_missing_wan_field_keeps_that_rules_item(unifi):
    _, state = run(unifi)
    del FakeUnifi.docs[SITE + "/stat/health"][0]["uptime_stats"]["WAN2"]["availability"]
    findings, state = run(unifi, state)
    [f] = findings
    assert f["check"] == "network" and "Cable Internet): no availability" in f["detail"]
    assert "wan_degraded:WAN2" in state["items"]
    FakeUnifi.docs = copy.deepcopy(DOCS)
    findings, _ = run(unifi, state)
    assert findings == [], "the same degradation back is not new"


def test_missing_zone_membership_keeps_the_isolation_item(unifi):
    _, state = run(unifi)
    FakeUnifi.docs[V2 + "/firewall/zone"][0]["network_ids"].remove("n52")
    findings, state = run(unifi, state)
    [f] = findings
    assert f["detail"] == "no zone holds network IoT" and state["items"]["isolation:IoT"] == "allowed by p30000"
    FakeUnifi.docs = copy.deepcopy(DOCS)
    assert run(unifi, state)[0] == []


def test_an_unread_network_list_keeps_the_latency_history(unifi):
    _, state = run(unifi)
    state["latency"]["WAN"] = [10.0] * 5
    FakeUnifi.fail = {SITE + "/rest/networkconf"}
    _, state = run(unifi, state)
    assert state["latency"]["WAN"] == [10.0] * 5 and "wan_down:WAN4" in state["items"]
    FakeUnifi.fail = set()
    FakeUnifi.docs[SITE + "/stat/health"][0]["uptime_stats"]["WAN"]["latency_average"] = 40
    findings, _ = run(unifi, state)
    assert reported(findings) == {"wan_latency:WAN"}


def test_a_latency_step_needs_five_nights_and_twice_the_median():
    w = {"wan": "WAN", "label": "Fiber", "status": "online", "up": True, "availability": 100.0, "latency": 35.0, "read": True}
    items, undecided, history, _ = nw.wan_items([w], {"WAN": [10.0, 11.0, 10.0, 12.0]})
    assert items == {} and undecided == {"wan_latency:WAN"} and history["WAN"] == [10.0, 11.0, 10.0, 12.0, 35.0]
    items, undecided, _, _ = nw.wan_items([w], {"WAN": [10.0, 11.0, 10.0, 12.0, 10.0]})
    assert items == {"wan_latency:WAN": {"text": "WAN (Fiber) 35 ms against a 5-night median of 10 ms", "value": "step"}} and undecided == set()
    items, _, _, _ = nw.wan_items([{**w, "latency": 25.0}], {"WAN": [10.0] * 5})
    assert items == {}, "twice the median but under median + 20 ms"
    _, _, history, _ = nw.wan_items([w], {"WAN": [10.0] * 14, "WAN9": [1.0]})
    assert len(history["WAN"]) == 14 and history["WAN"][-1] == 35.0 and "WAN9" not in history


def test_a_wan_with_no_state_is_an_item_and_keeps_its_history():
    w = {"wan": "WAN3", "label": "DSL", "status": None, "up": None, "availability": None, "latency": None, "read": False}
    items, undecided, history, problems = nw.wan_items([w], {"WAN3": [9.0]})
    assert items == {"wan_down:WAN3": {"text": "WAN3 (DSL): no state on the gateway", "value": "no state"}}
    assert undecided == {"wan_degraded:WAN3", "wan_latency:WAN3"} and history == {"WAN3": [9.0]} and problems == []


def test_ports_and_broad():
    assert nw.ports({"port": "53,853,1000-1002"}) == {53, 853, 1000, 1001, 1002}
    assert nw.any_port({}) and not nw.any_port({"port": "53"}) and not nw.any_port({"port_matching_type": "OBJECT", "port_group_id": "g1"})
    allow_all, ret = POLICIES[-1], POLICIES[-2]
    assert nw.broad(allow_all) and not nw.broad(ret) and not nw.broad(POLICIES[0])
    assert not nw.broad({**allow_all, "schedule": {"mode": "EVERY_DAY"}})
    assert not nw.broad({**allow_all, "destination": {**INTERNAL_ANY, "app_ids": ["rdp"]}})


def test_a_destination_application_is_a_destination_match():
    p = pol("p10030", 10030, "RDP by app", "ALLOW", RDP, {**INTERNAL_ANY, "app_ids": ["rdp"]})
    items, _, _ = nw.policy_items({"zones": ZONES, "policies": [p]})
    assert "source_port:p10030" not in items
    items, _, _ = nw.policy_items({"zones": ZONES, "policies": [{**p, "destination": INTERNAL_ANY}]})
    assert "source_port:p10030" in items


def test_dns_egress_needs_both_ports_blocked_for_all_of_the_internal_zone():
    def block(port, src=INTERNAL_ANY, protocol="tcp_udp"):
        return pol("p10010", 10010, "No outside DNS", "BLOCK", src, {"zone_id": "ze", "matching_target": "ANY", "port": port, "port_matching_type": "SPECIFIC"}, protocol=protocol)

    def dns(*extra):
        items, _, _ = nw.policy_items({"zones": ZONES, "policies": [*POLICIES, *extra]})
        return items.get("dns_egress", {}).get("value")

    assert dns() == "open 53 and 853"
    assert dns(block("53,853")) is None
    assert dns(block("53")) == "open 853"
    assert dns(block("53"), block("853")) is None
    assert dns(block("53,853", src={"zone_id": "zi", "matching_target": "NETWORK", "network_ids": ["n52"]})) == "open 53 and 853"
    assert dns(block("53,853", protocol="udp")) == "open 53 and 853"


def test_an_offline_camera_keeps_its_item_and_a_name_only_record_is_read(unifi):
    _, state = run(unifi)
    FakeUnifi.docs[SITE + "/stat/sta"].pop(0)
    findings, state = run(unifi, state)
    assert findings == [] and "camera_network:aa:bb:cc:00:00:01" in state["items"]
    items, undecided, _ = nw.camera_items({"networks": NETWORKS, "clients": [{"mac": "aa:bb:cc:00:00:02", "network": "Management"}]}, CAMERAS, "Cameras")
    assert set(items) == {"camera_network:aa:bb:cc:00:00:02"} and undecided == {"camera_network:aa:bb:cc:00:00:01", "camera_network:aa:bb:cc:00:00:03"}


def test_off_unconfigured_and_a_refused_login(unifi):
    assert nw.check({"unifi_url": ""}, None) == ([], None)
    [f], state = nw.check({"unifi_url": "http://127.0.0.1:9"}, None)
    assert f["severity"] == "warn" and f["title"] == "network checks: no UniFi account in the options" and state is None
    [f], state = nw.check({"unifi_username": "ro"}, None)
    assert f["title"] == "network checks: no UniFi account in the options", "absent url means the default, which still needs the account"
    [f], state = nw.check(opts(unifi, unifi_password="wrong"), None)
    assert f["title"] == "network checks: UniFi login failed: HTTPError: HTTP Error 401: Unauthorized" and state is None


class Ws:
    """Core's websocket: one Protect config entry, its devices by entry, others not."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def call(self, kind, **fields):
        if kind == "config_entries/get":
            assert fields == {"domain": "unifiprotect"}
            return [{"entry_id": "e1", "domain": "unifiprotect"}]
        if kind == "config/device_registry/list":
            return [
                {"name": "G4 Doorbell", "identifiers": [], "config_entries": ["e1"], "connections": [["mac", "AA:BB:CC:00:00:01"]]},
                {"name": "Lamp", "identifiers": [["zha", "x"]], "config_entries": ["e2"], "connections": [["mac", "11:22:33:44:55:66"]]},
                {"name": "Sensor", "identifiers": [], "config_entries": ["e1"], "connections": []},
            ]
        raise RuntimeError(kind)


def test_protect_macs_come_from_the_integrations_devices():
    assert nw.protect_macs(Ws()) == {"aa:bb:cc:00:00:01": "G4 Doorbell"}


def test_the_nightly_run_reports_network_items_and_serves_them(supervisor, unifi, tmp_path):  # noqa: F811
    loops = srv.Loops(options(**opts(unifi)), str(tmp_path), api=checks.Api("test-token", base=supervisor), now=lambda: NOW)
    loops.socket = Ws
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
    assert "pw" not in json.dumps(doc)
    later = loops.now() + dt.timedelta(days=1)
    loops.now = lambda: later
    assert not [f for f in loops.run_once()["findings"] if f["check"].startswith("network_")]
