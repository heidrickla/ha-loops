# Loops

Runs the health checks and the override mining once a day at `run_at`, and once at start if today's run has not happened. The panel shows the latest findings, the automations the household overrides most, and the run history.

| Severity | Meaning |
|---|---|
| action | something to do: a Supervisor issue or unhealthy state, no backup holding Home Assistant or a stale one (full, or partial with Home Assistant; an add-on's own backup does not count), Uptime Kuma monitors down, safety devices or locks newly unavailable |
| warn | worth a look: disk, a jump in unavailable entities, core log errors, an unsupported Supervisor, batteries newly under 20%, automation or script references to missing entities, an agent harness newly regressed (the Agent Harness Performance Tracker's regression sensor on, with the gates that fired), automations newly failing at run time and entities automations use newly unavailable (from Automation Monitor's sensors), custom integrations newly breaking now or within Breakage Radar's alert window, the agent-memory candidate review not run in 26 h, unable to reach its model or failing on a candidate, a check that failed |
| info | core and add-on updates, other updates newly available, newly unavailable entities, core log warnings, counts, Supervisor's `no_current_backup` (it counts full backups only, and Home Assistant's automatic backups are partial), custom integrations newly found by Breakage Radar using an API Home Assistant removes, with the earliest release |

Safety devices, batteries, other updates, broken references, harness regressions and the Automation Monitor and Breakage Radar findings are reported only when new since the previous run; a regression is reported again when another gate fires, and a failing automation when its failing step changes. The monitors check needs the Automation Monitor and Breakage Radar integrations; without their sensors it reports itself failed. Monitors named in `kuma_ignore` are left out of the down check.

An override is a manual change to an entity within `override_window_s` after an automation changed it, to a different state. The ranking counts them per automation over the last 30 days with the number of times the automation fired.

`POST /api/run` on the ingress port runs everything now; an override the corpus already holds is not stored again. `GET /api/snapshot.json` reads the unavailable entities and the ERROR/WARNING log signatures live, and answers 502 when a read fails. `stop_signatures` counts the occurrences logged in the last 15 seconds before Core exited, the stop's own noise.

`memory_url` (absent means `http://10.20.52.10:8765`, empty turns it off) is the agent-memory add-on whose nightly candidate review the run reports from its `/health`: an info line with what it forgot and how many candidates are left for agents, always in the phone summary.
