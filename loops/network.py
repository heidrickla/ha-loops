"""Nightly network checks against the UniFi controller, read with a read-only local account.

Each rule produces items under a stable key. An item is reported when its key is new or its
value changed since the previous run, and a key listed in `network_dismiss` is never reported.
A source that cannot be read is reported every run. Standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.cookiejar
import ipaddress
import json
import re
import ssl
import statistics
import urllib.error
import urllib.request
from collections.abc import Callable

UNIFI_URL = "https://10.20.52.1"
SITE = "/proxy/network/api/s/default"
V2 = "/proxy/network/v2/api/site/default"
ISOLATED = ("IoT", "Cameras", "Guest")
CAMERA_NETWORK = "Cameras"
AVAILABILITY_MIN = 99.0
LATENCY_NIGHTS = 14
LATENCY_MIN_NIGHTS = 5
LATENCY_STEP_MS = 20.0
DOWN = {"offline", "down", "disconnected", "failed", "error"}

# rule -> (severity, title noun)
RULES = {
    "wan_down": ("action", "WANs newly offline"),
    "wan_degraded": ("warn", f"WANs newly under {AVAILABILITY_MIN:g}% availability"),
    "wan_latency": ("warn", "WANs with a new latency step"),
    "isolation": ("warn", "networks that should be isolated newly reaching the internal zone"),
    "camera_network": ("warn", "Protect devices newly off the camera network"),
    "external_mac": ("warn", "new ALLOW policies from the internet matched on a MAC"),
    "source_port": ("warn", "new policies matching a source port with no destination port"),
    "duplicate_policy": ("info", "new duplicate firewall policies"),
    "same_name": ("info", "new firewall policies sharing another policy's name"),
    "dns_egress": ("info", "DNS to outside resolvers newly unblocked"),
    "port_forward": ("warn", "new port forwards"),
    "upnp": ("warn", "UPnP newly on"),
}

# Control characters, each made a space: clients name themselves, so any controller string can
# carry a newline or an escape into a finding.
CONTROL = {c: " " for c in (*range(32), *range(127, 160))}


def finding(check: str, severity: str, title: str, detail: str = "", value=None) -> dict:
    return {"check": check, "severity": severity, "title": title, "detail": detail, "value": value}


def plain(doc):
    if isinstance(doc, str):
        return doc.translate(CONTROL)
    if isinstance(doc, list):
        return [plain(x) for x in doc]
    if isinstance(doc, dict):
        return {k: plain(v) for k, v in doc.items()}
    return doc


class Unifi:
    """The controller API behind the UniFi OS web UI, with a login session."""

    def __init__(self, url: str, timeout: int = 45):
        # The gateway serves a self-signed certificate.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.csrf = ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def login(self, username: str, password: str) -> None:
        body = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(f"{self.url}/api/auth/login", data=body, method="POST", headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=self.timeout) as resp:
            self.csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get("X-Updated-CSRF-Token") or ""

    def get(self, path: str):
        with self.opener.open(urllib.request.Request(self.url + path), timeout=self.timeout) as resp:
            doc = plain(json.loads(resp.read()))
        return doc.get("data", doc) if isinstance(doc, dict) else doc

    def logout(self) -> None:
        req = urllib.request.Request(f"{self.url}/api/auth/logout", data=b"", method="POST", headers={"X-CSRF-Token": self.csrf})
        try:
            with self.opener.open(req, timeout=self.timeout):
                pass
        except (urllib.error.URLError, OSError):
            pass


SOURCES = {
    "networks": (SITE + "/rest/networkconf",),
    "health": (SITE + "/stat/health",),
    "devices": (SITE + "/stat/device",),
    "clients": (SITE + "/stat/sta",),
    "forwards": (SITE + "/rest/portforward",),
    "settings": (SITE + "/rest/setting",),
    "policies": (V2 + "/firewall-policies",),
    # The zone list that names each zone's networks; the matrix holds the zones without them.
    "zones": (V2 + "/firewall/zone", V2 + "/firewall/zones", V2 + "/firewall/zone-matrix"),
}


def read(client: Unifi) -> tuple[dict, dict[str, str], dict[str, str]]:
    """Every source, the error of each one that could not be read, and the path each was read from."""
    data: dict = {}
    errors: dict[str, str] = {}
    paths: dict[str, str] = {}
    for name, candidates in SOURCES.items():
        failed = ""
        for path in candidates:
            try:
                rows = client.get(path)
            except Exception as err:
                failed = failed or f"{type(err).__name__}: {err}"
                continue
            members = name == "zones" and any(isinstance(z, dict) and "network_ids" in z for z in rows or [])
            if name not in paths or members:
                data[name], paths[name] = rows, path
            if name != "zones" or members:
                break
        if name not in paths:
            errors[name] = failed
    return data, errors, paths


def shape(data: dict, paths: dict[str, str]) -> dict:
    """The field names of what was read, so a controller update that moves a field shows."""

    def keys(x) -> list[str]:
        return sorted(x) if isinstance(x, dict) else []

    def union(rows, pick=lambda r: r) -> list[str]:
        return sorted({k for r in rows or [] if isinstance(r, dict) and isinstance(pick(r), dict) for k in pick(r)})

    def first(rows) -> dict:
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}

    gw = gateway(data.get("devices") or []) or {}
    wan = next((h for h in data.get("health") or [] if h.get("subsystem") == "wan"), {})
    cell = first(first(data.get("zones")).get("data"))
    return {
        "paths": paths,
        "health_wan": keys(wan),
        "health_uptime_stats": keys(wan.get("uptime_stats")),
        "gateway": [k for k in keys(gw) if re.fullmatch(r"wan\d|last_wan_status|uptime_stats|wan_\w+", k)],
        "gateway_wan1": keys(gw.get("wan1")),
        "zone": keys(first(data.get("zones"))),
        "zone_data": keys(cell),
        "network_zone_fields": [k for k in union(data.get("networks")) if "zone" in k],
        "client_network_fields": [k for k in union(data.get("clients")) if k in ("network", "network_id", "vlan")],
        "policy": union(data.get("policies")),
        "policy_source": union(data.get("policies"), lambda p: p.get("source")),
        "policy_destination": union(data.get("policies"), lambda p: p.get("destination")),
    }


# ------------------------------------------------------------------ WANs


def gateway(devices: list[dict]) -> dict | None:
    return next((d for d in devices if d.get("type") in ("udm", "ugw", "uxg") or "wan1" in d), None)


def wans(data: dict) -> list[dict]:
    """One reading per WAN interface the controller has configured: WAN, WAN2, ..."""
    labels = {n.get("wan_networkgroup"): n.get("name") or "" for n in data.get("networks") or [] if n.get("purpose") == "wan" and n.get("enabled", True)}
    gw = gateway(data.get("devices") or []) or {}
    wan = next((h for h in data.get("health") or [] if h.get("subsystem") == "wan"), {})
    uptime = {**(gw.get("uptime_stats") or {}), **(wan.get("uptime_stats") or {})}
    status = gw.get("last_wan_status") or {}
    out = []
    for group in sorted(g for g in labels if g):
        m = re.fullmatch(r"WAN(\d*)", group)
        port = gw.get(f"wan{m.group(1) or 1}") if m else None
        stats = uptime.get(group) or {}
        latency = stats.get("latency_average", (port or {}).get("latency"))
        availability = stats.get("availability")
        out.append(
            {
                "wan": group,
                "label": labels[group],
                "status": status.get(group),
                "up": (port or {}).get("up"),
                "availability": float(availability) if isinstance(availability, int | float) else None,
                "latency": float(latency) if isinstance(latency, int | float) else None,
                "read": bool(stats) or group in status or port is not None,
            }
        )
    return out


WAN_RULES = ("wan_down", "wan_degraded", "wan_latency")


def wan_items(readings: list[dict], history: dict[str, list[float]]) -> tuple[dict, set[str], dict[str, list[float]], list[str]]:
    """Items, the keys left undecided, the latency history and what could not be read. A WAN the
    gateway holds no state for is an item; a missing field leaves its rule undecided for that WAN."""
    items: dict[str, dict] = {}
    undecided: set[str] = set()
    problems: list[str] = []
    configured = {w["wan"] for w in readings}
    new_history = {k: list(v) for k, v in history.items() if k in configured}
    for w in readings:
        wan = w["wan"]
        name = f"{wan} ({w['label']})" if w["label"] else wan
        if not w["read"]:
            items[f"wan_down:{wan}"] = {"text": f"{name}: no state on the gateway", "value": "no state"}
            undecided |= {f"wan_degraded:{wan}", f"wan_latency:{wan}"}
            continue
        state = str(w["status"] or "").lower()
        if state in DOWN or (not state and w["up"] is False):
            # a WAN that is down has no availability or latency of its own to judge
            items[f"wan_down:{wan}"] = {"text": f"{name} {state or 'link down'}", "value": "down"}
            undecided |= {f"wan_degraded:{wan}", f"wan_latency:{wan}"}
            continue
        if not state and not isinstance(w["up"], bool):
            undecided.add(f"wan_down:{wan}")
            problems.append(f"{name}: no status")
        if w["availability"] is None:
            undecided.add(f"wan_degraded:{wan}")
            problems.append(f"{name}: no availability")
        elif w["availability"] < AVAILABILITY_MIN:
            items[f"wan_degraded:{wan}"] = {"text": f"{name} {w['availability']:.2f}% available", "value": "degraded"}
        past = new_history.get(wan, [])
        if w["latency"] is None:
            undecided.add(f"wan_latency:{wan}")
            problems.append(f"{name}: no latency")
            continue
        if len(past) < LATENCY_MIN_NIGHTS:
            undecided.add(f"wan_latency:{wan}")
        else:
            median = statistics.median(past)
            if w["latency"] >= max(2 * median, median + LATENCY_STEP_MS):
                items[f"wan_latency:{wan}"] = {"text": f"{name} {w['latency']:g} ms against a {len(past)}-night median of {median:g} ms", "value": "step"}
        new_history[wan] = [*past, w["latency"]][-LATENCY_NIGHTS:]
    return items, undecided, new_history, problems


# ------------------------------------------------------------------ firewall


def zone_index(zones: list[dict], networks: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """Zones by id, and each network id's zone id, from the zone list or the network's own field."""
    by_id = {z.get("_id"): z for z in zones if isinstance(z, dict)}
    member = {}
    for zid, z in by_id.items():
        for n in z.get("network_ids") or []:
            member[n.get("_id") if isinstance(n, dict) else n] = zid
    for n in networks:
        zid = n.get("firewall_zone_id")
        if zid in by_id and n.get("_id") not in member:
            member[n.get("_id")] = zid
    return by_id, member


def zone_of_kind(zones: dict[str, dict], kind: str) -> str | None:
    return next((zid for zid, z in zones.items() if str(z.get("zone_key") or "").lower() == kind or str(z.get("name") or "").lower() == kind), None)


def any_port(end: dict) -> bool:
    kind = str(end.get("port_matching_type") or "ANY").upper()
    return kind == "ANY" and not str(end.get("port") or "").strip() and not end.get("port_group_id")


def ports(end: dict) -> set[int]:
    """The ports one side of a policy names; empty for any port or a port group."""
    out: set[int] = set()
    for part in str(end.get("port") or "").split(","):
        lo, _, hi = part.strip().partition("-")
        if lo.isdigit() and (not hi or hi.isdigit()):
            out.update(range(int(lo), int(hi or lo) + 1))
    return out


def always(p: dict) -> bool:
    """Every connection state, at all times."""
    schedule = (p.get("schedule") or {}).get("mode") or "ALWAYS"
    return str(p.get("connection_state_type") or "ALL").upper() == "ALL" and schedule.upper() == "ALWAYS"


def any_service(dst: dict) -> bool:
    """No destination port, port group, application or web domain."""
    return any_port(dst) and not dst.get("app_ids") and not dst.get("web_domains")


def broad(p: dict) -> bool:
    """Every protocol, port and connection state, at all times."""
    src, dst = p.get("source") or {}, p.get("destination") or {}
    return (
        str(p.get("protocol") or "all").lower() == "all"
        and not p.get("match_opposite_protocol")
        and any_port(src)
        and any_service(dst)
        and always(p)
        and not src.get("match_opposite_networks")
        and not dst.get("match_opposite_networks")
        and not dst.get("match_opposite_ips")
    )


def subnet(net: dict) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_interface(str(net.get("ip_subnet") or "")).network
    except ValueError:
        return None


def covers(dst: dict, others: list[dict]) -> bool:
    """The destination side reaches every other network of the internal zone."""
    target = str(dst.get("matching_target") or "ANY").upper()
    if target == "ANY":
        return True
    if target == "NETWORK":
        return {n.get("_id") for n in others} <= set(dst.get("network_ids") or [])
    if target == "IP":
        listed = []
        for ip in dst.get("ips") or []:
            with contextlib.suppress(ValueError):
                listed.append(ipaddress.ip_network(str(ip), strict=False))
        nets = [s for s in map(subnet, others) if s is not None]
        return all(any(s.version == n.version and s.subnet_of(n) for n in listed) for s in nets)
    return False


def decides(p: dict, nid: str, zid: str, internal: str, others: list[dict]) -> bool:
    """A broad policy from network `nid` (in zone `zid`) to all of the internal zone."""
    src, dst = p.get("source") or {}, p.get("destination") or {}
    target = str(src.get("matching_target") or "ANY").upper()
    if src.get("zone_id") != zid or dst.get("zone_id") != internal or not broad(p):
        return False
    return (target == "ANY" or (target == "NETWORK" and nid in (src.get("network_ids") or []))) and covers(dst, others)


def ordered(policies: list[dict]) -> list[dict]:
    """Enabled policies in evaluation order: user-made first, then predefined, each by index."""
    return sorted((p for p in policies if p.get("enabled")), key=lambda p: (bool(p.get("predefined")), p.get("index") or 0))


def isolation_items(data: dict, names: list[str]) -> tuple[dict, set[str], list[str]]:
    """A network to be isolated passes when UniFi isolates it, or when the first broad policy from
    it to the internal zone blocks. Narrower ALLOW policies before that one are its exceptions."""
    items: dict[str, dict] = {}
    undecided: set[str] = set()
    problems: list[str] = []
    networks = data.get("networks") or []
    by_name = {n.get("name"): n for n in networks}
    by_id = {n.get("_id"): n for n in networks}
    zones, member = zone_index(data.get("zones") or [], networks)
    internal = zone_of_kind(zones, "internal")
    if not internal or not member:
        return {}, {"isolation"}, ["no internal zone in the zone list" if not internal else "the zone list names no network's zone"]
    inside = {nid for nid, zid in member.items() if zid == internal}
    policies = ordered(data.get("policies") or [])
    for name in names:
        net = by_name.get(name)
        if not net:
            items[f"isolation:{name}"] = {"text": f"no network named {name}", "value": "missing"}
            continue
        if net.get("network_isolation_enabled"):
            continue
        nid, zid = net.get("_id"), member.get(net.get("_id"))
        if not zid:
            undecided.add(f"isolation:{name}")
            problems.append(f"no zone holds network {name}")
            continue
        others = [by_id[i] for i in inside - {nid} if i in by_id]
        decider = next((p for p in policies if decides(p, nid, zid, internal, others)), None)
        if decider is None:
            items[f"isolation:{name}"] = {"text": f"{name}: no policy decides its traffic to the internal zone", "value": "undecided"}
        elif str(decider.get("action") or "").upper() == "ALLOW":
            items[f"isolation:{name}"] = {"text": f"{name} reaches the internal zone through '{decider.get('name')}'", "value": f"allowed by {decider.get('_id')}"}
    return items, undecided, problems


# Fields that name a policy or count its use rather than say what it matches.
IDENTITY = {"_id", "index", "name", "description", "site_id", "origin_id", "predefined", "hits", "last_hit"}
DNS_PORTS = frozenset({53, 853})


def digest(p: dict) -> str:
    """What a policy matches and does, so a changed policy is reported again under the same key."""
    body = json.dumps({k: v for k, v in p.items() if k not in IDENTITY}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def open_dns(policies: list[dict], internal: str, external: str) -> set[int]:
    """The DNS ports no policy blocks from all of the internal zone to any internet address."""
    left = set(DNS_PORTS)
    for p in policies:
        src, dst = p.get("source") or {}, p.get("destination") or {}
        if (
            str(p.get("action") or "").upper() in ("BLOCK", "REJECT")
            and str(p.get("protocol") or "all").lower() in ("all", "tcp_udp")
            and not p.get("match_opposite_protocol")
            and always(p)
            and src.get("zone_id") == internal
            and str(src.get("matching_target") or "ANY").upper() == "ANY"
            and dst.get("zone_id") == external
            and str(dst.get("matching_target") or "ANY").upper() == "ANY"
            and not dst.get("match_opposite_ports")
        ):
            left -= set(DNS_PORTS) if any_port(dst) else ports(dst)
    return left


def policy_items(data: dict) -> tuple[dict, set[str], list[str]]:
    items: dict[str, dict] = {}
    zones, _ = zone_index(data.get("zones") or [], data.get("networks") or [])
    external, internal = zone_of_kind(zones, "external"), zone_of_kind(zones, "internal")
    enabled = ordered(data.get("policies") or [])
    user = [p for p in enabled if not p.get("predefined")]
    for p in user:
        src, dst = p.get("source") or {}, p.get("destination") or {}
        pid, name = p.get("_id"), p.get("name") or "-"
        target = str(src.get("matching_target") or "ANY").upper()
        if str(p.get("action") or "").upper() == "ALLOW" and external and src.get("zone_id") == external and (target == "MAC" or src.get("macs") or src.get("mac_addresses")):
            items[f"external_mac:{pid}"] = {"text": f"'{name}' (index {p.get('index')})", "value": digest(p)}
        if not any_port(src) and any_service(dst):
            items[f"source_port:{pid}"] = {"text": f"'{name}' (index {p.get('index')}) source port {src.get('port')}", "value": digest(p)}
    seen: dict[str, dict] = {}
    named: dict[str, dict] = {}
    for p in user:
        sig = digest(p)
        if sig in seen:
            items[f"duplicate_policy:{p.get('_id')}"] = {"text": f"'{p.get('name')}' (index {p.get('index')}) repeats index {seen[sig].get('index')}", "value": sig}
        else:
            seen[sig] = p
        # One policy made for several zone pairs is several policies under one name and index.
        first = named.setdefault(str(p.get("name") or ""), p)
        if p.get("name") and first is not p and first.get("index") != p.get("index") and sig != digest(first):
            items[f"same_name:{p.get('_id')}"] = {"text": f"'{p.get('name')}' (index {p.get('index')}) shares the name of index {first.get('index')}", "value": sig}
    if not external or not internal:
        return items, {"external_mac", "dns_egress"}, ["no internal or external zone in the zone list"]
    left = open_dns(enabled, internal, external)
    if left:
        named = " and ".join(str(n) for n in sorted(left))
        items["dns_egress"] = {"text": f"no policy blocks port {named} from the internal zone to the internet", "value": f"open {named}"}
    return items, set(), []


def forward_items(data: dict) -> dict:
    items = {}
    for r in data.get("forwards") or []:
        if r.get("enabled", True):
            desc = f"{r.get('proto') or '-'} wan:{r.get('dst_port') or '-'} -> {r.get('fwd') or '-'}:{r.get('fwd_port') or '-'} from {r.get('src') or 'any'}"
            items[f"port_forward:{r.get('_id')}"] = {"text": f"'{r.get('name') or '-'}' {desc}", "value": desc}
    return items


def upnp_items(data: dict) -> dict:
    usg = next((s for s in data.get("settings") or [] if s.get("key") == "usg"), {})
    return {"upnp": {"text": "UPnP is enabled on the gateway", "value": "on"}} if usg.get("upnp_enabled") else {}


def camera_items(data: dict, cameras: dict[str, str], network: str) -> tuple[dict, set[str], list[str]]:
    """Protect devices, by MAC from Home Assistant's device registry, connected anywhere but
    `network`. A device not connected now keeps what it had."""
    nets = {n.get("name"): n.get("_id") for n in data.get("networks") or []}
    if network not in nets:
        return {f"camera_network:{network}": {"text": f"no network named {network}", "value": "missing"}}, set(), []
    clients = {str(c.get("mac") or "").lower(): c for c in data.get("clients") or []}
    items: dict[str, dict] = {}
    undecided: set[str] = set()
    problems: list[str] = []
    for mac, name in cameras.items():
        c = clients.get(mac)
        key = f"camera_network:{mac}"
        if c is None:
            undecided.add(key)
        elif c.get("network_id") is None and c.get("network") is None:
            undecided.add(key)
            problems.append(f"{name}: no network on its client record")
        elif (c.get("network_id") or nets.get(c.get("network"))) != nets[network]:
            on = c.get("network") or c.get("network_id")
            items[key] = {"text": f"{name} ({mac}) on {on}", "value": on}
    return items, undecided, problems


def protect_macs(ws) -> dict[str, str]:
    """MAC -> name of every device the UniFi Protect integration registered."""
    entries = {e.get("entry_id") for e in ws.call("config_entries/get", domain="unifiprotect") or []}
    out = {}
    for d in ws.call("config/device_registry/list") or []:
        if entries & set(d.get("config_entries") or []):
            for kind, value in d.get("connections") or []:
                if kind == "mac":
                    out[str(value).lower()] = d.get("name_by_user") or d.get("name") or str(value)
    return out


# ------------------------------------------------------------------ the check


def report(items: dict[str, dict], previous: dict[str, str] | None, dismiss: set[str]) -> list[dict]:
    """One finding per rule, naming the items new or changed since the previous run."""
    out = []
    for rule, (severity, noun) in RULES.items():
        new = {k: v for k, v in items.items() if k.split(":", 1)[0] == rule and k not in dismiss and (previous is None or previous.get(k) != v["value"])}
        if not new:
            continue
        first = " (first run: everything current)" if previous is None else ""
        detail = "; ".join(f"{v['text']} [dismiss: {k}]" for k, v in sorted(new.items()))
        out.append(finding(f"network_{rule}", severity, f"{len(new)} {noun}{first}", detail, sorted(new)))
    return out


def evaluate(data: dict, errors: dict[str, str], options: dict, history: dict, cameras: Callable[[], dict[str, str]] | None) -> tuple[dict, set[str], dict, list[str]]:
    """Every rule over what was read: items, the keys or whole rules left undecided, the latency
    history and the problems. A rule whose source could not be read is undecided."""
    items: dict[str, dict] = {}
    undecided: set[str] = set()
    problems = [f"{name}: {err}" for name, err in sorted(errors.items())]
    readings = wans(data)
    if "networks" in errors or {"health", "devices"} <= errors.keys():
        undecided.update(WAN_RULES)
    elif not readings:
        problems.append("no WAN in the network list")
    else:
        found, und, history, trouble = wan_items(readings, history)
        items.update(found)
        undecided |= und
        problems += trouble
    if {"networks", "zones", "policies"} & errors.keys():
        undecided |= {"isolation", "external_mac", "source_port", "duplicate_policy", "same_name", "dns_egress"}
    else:
        names = [n for n in options.get("isolated_networks", list(ISOLATED)) or [] if n]
        for found, und, trouble in (isolation_items(data, names), policy_items(data)):
            items.update(found)
            undecided |= und
            problems += trouble
    if "forwards" in errors:
        undecided.add("port_forward")
    else:
        items.update(forward_items(data))
    if "settings" in errors:
        undecided.add("upnp")
    else:
        items.update(upnp_items(data))
    network = str(options.get("camera_network", CAMERA_NETWORK) or "")
    if network and cameras:
        if {"networks", "clients"} & errors.keys():
            undecided.add("camera_network")
        else:
            try:
                macs = cameras()
            except Exception as err:
                undecided.add("camera_network")
                problems.append(f"Protect devices: {type(err).__name__}: {err}")
            else:
                found, und, trouble = camera_items(data, macs, network)
                items.update(found)
                undecided |= und
                problems += trouble
    return items, undecided, history, problems


def check(options: dict, previous: dict | None, cameras: Callable[[], dict[str, str]] | None = None) -> tuple[list[dict], dict | None]:
    """The findings, and the state the next run compares against; None keeps the previous state."""
    url = str(options.get("unifi_url", UNIFI_URL) or "")
    if not url:
        return [], None
    user, password = options.get("unifi_username") or "", options.get("unifi_password") or ""
    if not user or not password:
        return [finding("network", "warn", "network checks: no UniFi account in the options")], None
    client = Unifi(url)
    try:
        client.login(user, password)
    except Exception as err:
        return [finding("network", "warn", f"network checks: UniFi login failed: {type(err).__name__}: {err}")], None
    try:
        data, errors, paths = read(client)
    finally:
        client.logout()
    previous = previous or {}
    items, undecided, history, problems = evaluate(data, errors, options, previous.get("latency") or {}, cameras)
    dismiss = {k for k in options.get("network_dismiss") or [] if k}
    before = previous.get("items") if "items" in previous else None
    out = report(items, before, dismiss)
    if problems:
        out.append(finding("network", "warn", f"network checks: {len(problems)} not read", "; ".join(problems), problems))
    # An item whose rule could not decide it this run stays as it was, so it is neither dropped
    # nor reported as new when the reading comes back.
    kept = {k: v for k, v in (before or {}).items() if (k in undecided or k.split(":", 1)[0] in undecided) and k not in dismiss}
    state = {
        "items": {**kept, **{k: v["value"] for k, v in items.items() if k not in dismiss}},
        "texts": {k: v["text"] for k, v in items.items()},
        "dismissed": sorted(dismiss & items.keys()),
        "undecided": sorted(undecided),
        "wans": wans(data),
        "latency": history,
        "shape": shape(data, paths),
    }
    return out, state
