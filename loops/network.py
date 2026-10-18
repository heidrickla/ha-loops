"""Nightly network checks against the UniFi controller, read with a read-only local account.

Each rule produces items under a stable key. An item is reported when its key is new or its
value changed since the previous run, and a key listed in `network_dismiss` is never reported.
A source that cannot be read is reported every run. Standard library only.
"""

from __future__ import annotations

import contextlib
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
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
# Fields that name a policy rather than say what it matches.
IDENTITY = {"_id", "index", "name", "description", "site_id", "origin_id", "predefined"}

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
    "networks": SITE + "/rest/networkconf",
    "health": SITE + "/stat/health",
    "devices": SITE + "/stat/device",
    "clients": SITE + "/stat/sta",
    "forwards": SITE + "/rest/portforward",
    "settings": SITE + "/rest/setting",
    "policies": V2 + "/firewall-policies",
    "zones": V2 + "/firewall/zone-matrix",
}


def read(client: Unifi) -> tuple[dict, dict[str, str]]:
    """Every source, and the error of each one that could not be read."""
    data: dict = {}
    errors: dict[str, str] = {}
    for name, path in SOURCES.items():
        # an older controller has the zone list without the matrix
        for p in (path, V2 + "/firewall/zones") if name == "zones" else (path,):
            try:
                data[name] = client.get(p)
                errors.pop(name, None)
                break
            except Exception as err:
                errors[name] = f"{type(err).__name__}: {err}"
    return data, errors


def shape(data: dict) -> dict:
    """The field names of what was read, so a controller update that moves a field shows."""

    def keys(x) -> list[str]:
        return sorted(x) if isinstance(x, dict) else []

    gw = gateway(data.get("devices") or [])
    wan = next((h for h in data.get("health") or [] if h.get("subsystem") == "wan"), {})

    def first(rows) -> dict:
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}

    return {
        "health_wan": keys(wan),
        "health_uptime_stats": keys(wan.get("uptime_stats")),
        "gateway": [k for k in keys(gw) if re.fullmatch(r"wan\d|last_wan_status|uptime_stats|wan_\w+", k)],
        "gateway_wan1": keys((gw or {}).get("wan1")),
        "zone": keys(first(data.get("zones"))),
        "policy": keys(first(data.get("policies"))),
        "policy_source": keys(first(data.get("policies")).get("source")),
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
        out.append(
            {
                "wan": group,
                "label": labels[group],
                "status": status.get(group),
                "up": (port or {}).get("up"),
                "availability": stats.get("availability"),
                "latency": float(latency) if isinstance(latency, int | float) else None,
                "read": bool(stats) or group in status or port is not None,
            }
        )
    return out


def wan_items(readings: list[dict], history: dict[str, list[float]]) -> tuple[dict, dict[str, list[float]], list[str]]:
    items: dict[str, dict] = {}
    unread = []
    new_history = {}
    for w in readings:
        name = f"{w['wan']} ({w['label']})" if w["label"] else w["wan"]
        past = list(history.get(w["wan"]) or [])
        if not w["read"]:
            unread.append(w["wan"])
            new_history[w["wan"]] = past
            continue
        state = str(w["status"] or "").lower()
        if state in DOWN or (not state and w["up"] is False):
            items[f"wan_down:{w['wan']}"] = {"text": f"{name} {state or 'link down'}", "value": "down"}
        if isinstance(w["availability"], int | float) and w["availability"] < AVAILABILITY_MIN:
            items[f"wan_degraded:{w['wan']}"] = {"text": f"{name} {w['availability']:.2f}% available", "value": "degraded"}
        if w["latency"] is not None:
            if len(past) >= LATENCY_MIN_NIGHTS:
                median = statistics.median(past)
                if w["latency"] >= max(2 * median, median + LATENCY_STEP_MS):
                    items[f"wan_latency:{w['wan']}"] = {"text": f"{name} {w['latency']:g} ms against a {len(past)}-night median of {median:g} ms", "value": "step"}
            past = [*past, w["latency"]][-LATENCY_NIGHTS:]
        new_history[w["wan"]] = past
    return items, new_history, unread


# ------------------------------------------------------------------ firewall


def zone_index(zones: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """Zones by id, and each network id's zone id."""
    by_id = {z.get("_id"): z for z in zones if isinstance(z, dict)}
    member = {}
    for zid, z in by_id.items():
        for n in z.get("network_ids") or z.get("networks") or []:
            member[n.get("_id") if isinstance(n, dict) else n] = zid
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


def broad(p: dict) -> bool:
    """Every protocol, port and connection state, at all times."""
    src, dst = p.get("source") or {}, p.get("destination") or {}
    schedule = (p.get("schedule") or {}).get("mode") or "ALWAYS"
    return (
        str(p.get("protocol") or "all").lower() == "all"
        and any_port(src)
        and any_port(dst)
        and str(p.get("connection_state_type") or "ALL").upper() == "ALL"
        and schedule.upper() == "ALWAYS"
        and not src.get("match_opposite_networks")
        and not dst.get("match_opposite_networks")
        and not dst.get("match_opposite_ips")
    )


def covers_internal(dst: dict, others: set[str]) -> bool:
    target = str(dst.get("matching_target") or "ANY").upper()
    if target == "ANY":
        return True
    if target == "NETWORK":
        return others <= set(dst.get("network_ids") or [])
    if target == "IP":
        nets = []
        for ip in dst.get("ips") or []:
            with contextlib.suppress(ValueError):
                nets.append(ipaddress.ip_network(str(ip), strict=False))
        return all(any(n.version == p.version and p.subnet_of(n) for n in nets) for p in PRIVATE)
    return False


def decides(p: dict, nid: str, zid: str, internal: str, others: set[str]) -> bool:
    """A broad policy from network `nid` (in zone `zid`) to all of the internal zone."""
    src, dst = p.get("source") or {}, p.get("destination") or {}
    target = str(src.get("matching_target") or "ANY").upper()
    if src.get("zone_id") != zid or dst.get("zone_id") != internal or not broad(p):
        return False
    return (target == "ANY" or (target == "NETWORK" and nid in (src.get("network_ids") or []))) and covers_internal(dst, others)


def isolation_items(data: dict, names: list[str]) -> tuple[dict, list[str]]:
    """A network to be isolated passes when UniFi isolates it, or when the first broad policy from
    it to the internal zone blocks. Narrower ALLOW policies before that one are its exceptions."""
    items: dict[str, dict] = {}
    problems = []
    nets = {n.get("name"): n for n in data.get("networks") or []}
    zones, member = zone_index(data.get("zones") or [])
    internal = zone_of_kind(zones, "internal")
    if not internal:
        return items, ["no internal zone in the zone list"]
    inside = {nid for nid, zid in member.items() if zid == internal}
    policies = sorted((p for p in data.get("policies") or [] if p.get("enabled")), key=lambda p: (bool(p.get("predefined")), p.get("index") or 0))
    for name in names:
        net = nets.get(name)
        if not net:
            items[f"isolation:{name}"] = {"text": f"no network named {name}", "value": "missing"}
            continue
        if net.get("network_isolation_enabled"):
            continue
        nid, zid = net.get("_id"), member.get(net.get("_id"))
        if not zid:
            problems.append(f"no zone holds network {name}")
            continue
        decider = next((p for p in policies if decides(p, nid, zid, internal, inside - {nid})), None)
        if decider is None:
            items[f"isolation:{name}"] = {"text": f"{name}: no policy decides its traffic to the internal zone", "value": "undecided"}
        elif str(decider.get("action") or "").upper() == "ALLOW":
            items[f"isolation:{name}"] = {"text": f"{name} reaches the internal zone through '{decider.get('name')}'", "value": f"allowed by {decider.get('name')}"}
    return items, problems


def policy_items(data: dict) -> dict:
    items: dict[str, dict] = {}
    zones, _ = zone_index(data.get("zones") or [])
    external = zone_of_kind(zones, "external")
    user = sorted((p for p in data.get("policies") or [] if p.get("enabled") and not p.get("predefined")), key=lambda p: p.get("index") or 0)
    for p in user:
        src, dst = p.get("source") or {}, p.get("destination") or {}
        pid, name = p.get("_id"), p.get("name") or "-"
        target = str(src.get("matching_target") or "ANY").upper()
        if str(p.get("action") or "").upper() == "ALLOW" and external and src.get("zone_id") == external and (target == "MAC" or src.get("mac_addresses")):
            items[f"external_mac:{pid}"] = {"text": f"'{name}' (index {p.get('index')})", "value": name}
        if not any_port(src) and any_port(dst):
            items[f"source_port:{pid}"] = {"text": f"'{name}' (index {p.get('index')}) source port {src.get('port')}", "value": name}
    seen: dict[str, dict] = {}
    for p in user:
        sig = json.dumps({k: v for k, v in p.items() if k not in IDENTITY}, sort_keys=True)
        if sig in seen:
            first = seen[sig]
            items[f"duplicate_policy:{p.get('_id')}"] = {"text": f"'{p.get('name')}' (index {p.get('index')}) repeats index {first.get('index')}", "value": p.get("name") or "-"}
        else:
            seen[sig] = p
    if external:
        dns = {53, 853}
        blocked = any(
            str(p.get("action") or "").upper() in ("BLOCK", "REJECT") and (p.get("destination") or {}).get("zone_id") == external and dns & ports(p.get("destination") or {})
            for p in data.get("policies") or []
            if p.get("enabled")
        )
        if not blocked:
            items["dns_egress"] = {"text": "no policy blocks port 53 or 853 to the internet", "value": "open"}
    return items


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


def camera_items(data: dict, cameras: dict[str, str], network: str) -> dict:
    """Protect devices, by MAC from Home Assistant's device registry, connected anywhere but `network`."""
    nets = {n.get("name"): n.get("_id") for n in data.get("networks") or []}
    if network not in nets:
        return {f"camera_network:{network}": {"text": f"no network named {network}", "value": "missing"}}
    items = {}
    for c in data.get("clients") or []:
        mac = str(c.get("mac") or "").lower()
        if mac in cameras and c.get("network_id") != nets[network]:
            on = c.get("network") or c.get("network_id") or "?"
            items[f"camera_network:{mac}"] = {"text": f"{cameras[mac]} ({mac}) on {on}", "value": on}
    return items


def protect_macs(ws) -> dict[str, str]:
    """MAC -> name of every device the UniFi Protect integration registered."""
    out = {}
    for d in ws.call("config/device_registry/list") or []:
        if any(i and i[0] == "unifiprotect" for i in d.get("identifiers") or []):
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
        data, errors = read(client)
    finally:
        client.logout()
    previous = previous or {}
    problems = [f"{name}: {err}" for name, err in sorted(errors.items())]
    readings = wans(data)
    items, history, unread = wan_items(readings, previous.get("latency") or {})
    evaluated = {"wan_down", "wan_degraded", "wan_latency"} if readings else set()
    if not readings:
        problems.append("no WAN in the network list")
    problems += [f"no state for {w}" for w in unread]
    names = [n for n in options.get("isolated_networks", list(ISOLATED)) or [] if n]
    if {"networks", "zones", "policies"}.isdisjoint(errors):
        iso, trouble = isolation_items(data, names)
        items.update(iso)
        problems += trouble
        items.update(policy_items(data))
        evaluated |= {"isolation", "external_mac", "source_port", "duplicate_policy", "dns_egress"}
    if "forwards" not in errors:
        items.update(forward_items(data))
        evaluated.add("port_forward")
    if "settings" not in errors:
        items.update(upnp_items(data))
        evaluated.add("upnp")
    network = str(options.get("camera_network", CAMERA_NETWORK) or "")
    if network and cameras and {"networks", "clients"}.isdisjoint(errors):
        try:
            items.update(camera_items(data, cameras(), network))
            evaluated.add("camera_network")
        except Exception as err:
            problems.append(f"Protect devices: {type(err).__name__}: {err}")
    dismiss = {k for k in options.get("network_dismiss") or [] if k}
    before = previous.get("items") if "items" in previous else None
    out = report(items, before, dismiss)
    if problems:
        out.append(finding("network", "warn", f"network checks: {len(problems)} reads failed", "; ".join(problems), problems))
    # An item of a rule that could not be evaluated, or of a WAN that could not be read, stays as it
    # was, so it is not reported as new when the source comes back.
    kept = {k: v for k, v in (before or {}).items() if k.split(":", 1)[0] not in evaluated or k.split(":", 1)[-1] in unread}
    state = {
        "items": {**kept, **{k: v["value"] for k, v in items.items() if k not in dismiss}},
        "texts": {k: v["text"] for k, v in items.items()},
        "dismissed": sorted(dismiss & items.keys()),
        "wans": readings,
        "latency": history,
        "shape": shape(data),
    }
    return out, state
