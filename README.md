# ha-loops

Home Assistant add-on hosting the recurring improvement loops, so they run on the always-on system rather than on a desktop.

| Loop | What runs nightly | What persists |
|---|---|---|
| Health checks | core and add-on updates, Supervisor issues, backup age, host disk, unavailable-entity delta, core log errors, Uptime Kuma monitors down; newly unavailable safety devices and locks, batteries newly under 20%, other pending updates, automation and script references to missing entities, Agent Harness Performance Tracker regressions; from Automation Monitor and Breakage Radar, automations failing at run time, entities automations use gone unavailable, custom integrations using an API Home Assistant removes | `/data/findings/<date>.json`, `latest.json`, `history.json`; a summary to the phone |
| Override corpus | the last day of the logbook, every manual change that undid an automation's change within the window | `/data/overrides.jsonl`, daily automation fire counts, the ranking on the panel and at `/api/overrides.json` |
| HVAC per degree-day (loop 7) | each new local day's hourly HVAC circuit energy and outdoor temperature from Core's long-term statistics; kWh per CDD65 per zone and week | `/data/controller` (inputs, days, weeks), `/api/controller.json`, the page's weeks table |
| Manual actions (loop 4) | live: every change of an actionable entity and every change a person made; nightly: baseline policies scored on held-out days | `/data/stream`, `/data/actions`, `/data/gaps.jsonl`, `/api/actions.json` |

Rule for the check suite: a finding that is fixed leaves its check behind. The suite only grows.

## Layout

| Path | Purpose |
|---|---|
| `loops/config.yaml` | add-on manifest: `hassio_role: manager`, `homeassistant_api`, ingress on 8095 |
| `loops/server.py` | scheduler, store, ingress server, `/api/run` (POST) to run now |
| `loops/checks.py` | the check suite; each check returns findings, a failing check is itself a finding |
| `loops/overrides.py` | override detection and the corpus |
| `loops/controller.py` | loop 7: HVAC energy per cooling degree-day |
| `loops/collector.py` | loop 4: the manual-action record and its baseline score |
| `loops/hass_ws.py` | Core's websocket API, standard library |
| `loops/page.py` | the dark ingress page |
| `tests/` | the suite against a fake Supervisor, the server end to end |

## Options

| Option | Default | Meaning |
|---|---|---|
| `run_at` | `03:30` | local time of the nightly run |
| `notify_target` | empty | notify service name for the summary |
| `override_window_s` | 300 | manual change this soon after an automation's counts as an override |
| `backup_max_age_days` | 7 | older newest backup is a finding |
| `disk_warn_percent` | 85 | host disk threshold |
| `unavailable_warn_delta` | 25 | more unavailable entities than the last run by this many is a warning |
| `log_lines` | 500 | core log lines scanned |
| `kuma_url`, `kuma_api_key` | empty | optional; down monitors become findings |
| `kuma_ignore` | empty | monitor names left out of the down check, for machines that are off unless needed |
| `hvac_zones` | empty | `name=statistic_id[,statistic_id]` per zone: the energy statistics of the circuits that run it |
| `outdoor_temperature` | empty | statistic id of an outdoor temperature with hourly means |
| `controller_arm` | `baseline` | label of the thermostat settings in force, recorded per day |
| `controller_from` | empty | first local day measured, `YYYY-MM-DD`; empty is the last 30 days |
| `energy_meter` | empty | statistic id of the panel's total energy; an hour it did not move counts every circuit's hour as missing |

## Endpoints

| Path | Answers |
|---|---|
| `/` | the page |
| `/healthz` | `ok`, for the watchdog |
| `/api/latest.json` | the latest run's findings |
| `/api/history.json` | the run history |
| `/api/overrides.json` | the override ranking |
| `/api/snapshot.json` | unavailable or unknown entities and ERROR/WARNING log signatures, read live, with `stop_signatures`: the occurrences logged in the last 15 s before Core exited; 502 when a read fails |
| `/api/controller.json` | the last 60 HVAC days with their inputs' counts, and every week |
| `/api/actions.json` | the collector's connection, its gaps and the latest score |
| `POST /api/run` | runs everything now |

## Develop

```
python -m pytest -q tests
python -m ruff check . && python -m ruff format --check .
```

The forge workflow `.gitea/workflows/validate.yml` runs these plus yamllint and shellcheck.

Deploy to a local add-on directory: `git archive --format=tar HEAD loops | ssh root@<ha> 'tar -x -C /local_apps && ha store reload'`, then install or rebuild `local_loops`.
