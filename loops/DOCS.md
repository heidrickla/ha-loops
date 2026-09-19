# Loops

Runs the health checks and the override mining once a day at `run_at`, and once at
start if today's run has not happened. The panel shows the latest findings, the
automations the household overrides most, and the run history.

| Severity | Meaning |
|---|---|
| action | something to do: a Supervisor issue, a stale backup, monitors down |
| warn | worth a look: disk, a jump in unavailable entities, core errors |
| info | updates available, newly unavailable entities, counts |

An override is a manual change to an entity within `override_window_s` after an
automation changed it, to a different state. The ranking counts them per
automation over the last 30 days with the number of times the automation fired.

`POST /api/run` on the ingress port runs everything now.
