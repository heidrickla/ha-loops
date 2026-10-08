"""The ingress page: the latest findings, the override ranking, the run history. Dark."""

from __future__ import annotations

import html

CSS = """:root{color-scheme:dark}body{font:15px system-ui,sans-serif;margin:1.5rem;max-width:64rem;background:#111418;color:#e3e6ea}
h1{font-size:1.4rem;margin:0 0 .2rem}h2{font-size:1.1rem;margin:1.6rem 0 .5rem}p.sub{color:#9aa4b1;margin:0 0 1rem}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #2c3138;padding:.4rem .5rem;text-align:left;vertical-align:top}
th{color:#9aa4b1;font-weight:600}code{font-size:.9em;color:#c9d1d9}.pill{padding:.1rem .5rem;border-radius:.6rem;background:#2a2f36;white-space:nowrap}
.action{background:#7a2b2b;color:#fff}.warn{background:#6b5a1e;color:#fff}.info{background:#2a2f36}.muted{color:#9aa4b1}"""


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def render(latest: dict | None, ranking: list[dict], history: list[dict], weeks: list[dict] | None = None) -> str:
    parts = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Loops</title><style>{CSS}</style>"]
    parts.append("<h1>Loops</h1><p class=sub>Nightly checks whose findings stay as checks, and the automations the household overrides.</p>")
    if latest:
        parts.append(f"<h2>Findings, run {esc(latest.get('ran_at', ''))[:16]}</h2>")
        rows = latest.get("findings", [])
        if not rows:
            parts.append("<p class=muted>Nothing to report.</p>")
        else:
            parts.append("<table><tr><th>Severity</th><th>Check</th><th>Finding</th><th>Detail</th></tr>")
            for f in sorted(rows, key=lambda f: {"action": 0, "warn": 1, "info": 2}.get(f["severity"], 3)):
                parts.append(
                    f"<tr><td><span class='pill {esc(f['severity'])}'>{esc(f['severity'])}</span></td><td><code>{esc(f['check'])}</code></td><td>{esc(f['title'])}</td><td class=muted>{esc(f['detail'])[:300]}</td></tr>"
                )
            parts.append("</table>")
    else:
        parts.append("<h2>Findings</h2><p class=muted>No run yet.</p>")
    parts.append("<h2>Overrides, last 30 days</h2>")
    if not ranking:
        parts.append("<p class=muted>No manual change has undone an automation within the window.</p>")
    else:
        parts.append("<table><tr><th>Overrides</th><th>Fired</th><th>Automation</th><th>Entities</th><th>Last</th></tr>")
        for a in ranking[:30]:
            ents = ", ".join(f"{e} x{n}" for e, n in a["entities"].items())
            parts.append(
                f"<tr><td>{a['overrides']}</td><td class=muted>{a['fired'] or ''}</td><td>{esc(a['name'] or a['automation'])}<br><code>{esc(a['automation'])}</code></td><td class=muted>{esc(ents)}</td><td class=muted>{esc(a['last'])[:16]}</td></tr>"
            )
        parts.append("</table>")
    parts.append("<h2>HVAC energy per cooling degree-day</h2>")
    if not weeks:
        parts.append("<p class=muted>Not measured yet: set hvac_zones and outdoor_temperature.</p>")
    else:
        names = list(weeks[-1]["zones"])
        parts.append("<table><tr><th>Week</th><th>Arm</th>" + "".join(f"<th>{esc(n)} kWh/CDD65</th>" for n in names) + "</tr>")
        for w in reversed(weeks[-26:]):
            cells = []
            for n in names:
                z = w["zones"].get(n) or {}
                if z.get("kwh_per_cdd") is None:
                    cells.append(f"<td class=muted>{esc(z.get('reason', 'no data'))}</td>")
                else:
                    cells.append(f"<td>{z['kwh_per_cdd']} <span class=muted>({z['kwh']} kWh / {z['cdd65']}, {z['days_counted']}d)</span></td>")
            parts.append(f"<tr><td>{esc(w['week'])}</td><td class=muted>{esc(', '.join(w['arms']))}</td>{''.join(cells)}</tr>")
        parts.append("</table>")
    parts.append("<h2>Runs</h2>")
    if history:
        parts.append("<table><tr><th>Run</th><th>Action</th><th>Warn</th><th>Info</th><th>Overrides found</th><th>Seconds</th></tr>")
        for h in reversed(history[-30:]):
            c = h.get("counts", {})
            parts.append(
                f"<tr><td>{esc(h.get('ran_at', ''))[:16]}</td><td>{c.get('action', 0)}</td><td>{c.get('warn', 0)}</td><td>{c.get('info', 0)}</td><td>{h.get('overrides', 0)}</td><td class=muted>{h.get('seconds', '')}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p class=muted>No runs recorded.</p>")
    return "".join(parts)
