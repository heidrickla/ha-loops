# Loops

Runs the health checks and the override mining once a day at `run_at`, and once at start if today's run has not happened. The panel shows the latest findings, the automations the household overrides most, and the run history.

| Severity | Meaning |
|---|---|
| action | something to do: a Supervisor issue or unhealthy state, no backup or a stale one, Uptime Kuma monitors down, safety devices or locks newly unavailable |
| warn | worth a look: disk, a jump in unavailable entities, core log errors, an unsupported Supervisor, batteries newly under 20%, automation or script references to missing entities, an agent harness newly regressed (the Agent Harness Performance Tracker's regression sensor on, with the gates that fired), a check that failed |
| info | core and add-on updates, other updates newly available, newly unavailable entities, core log warnings, counts |

Safety devices, batteries, other updates, broken references and harness regressions are reported only when new since the previous run; a regression is reported again when another gate fires. Monitors named in `kuma_ignore` are left out of the down check.

An override is a manual change to an entity within `override_window_s` after an automation changed it, to a different state. The ranking counts them per automation over the last 30 days with the number of times the automation fired.

`POST /api/run` on the ingress port runs everything now. `GET /api/snapshot.json` reads the unavailable entities and the ERROR/WARNING log signatures live, and answers 502 when a read fails.
