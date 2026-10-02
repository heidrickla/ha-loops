# Changelog

## 0.6.0

- The nightly run reports the agent-memory add-on's candidate review from its `/health` (`memory_url`): info with what it forgot and the candidates left for agents, on its own line in the phone summary; warn when it has not run in 26 h, could not reach its model or failed on a candidate.

## 0.5.0

- The nightly run reads Automation Monitor's failed-automations and linked-entities sensors and Breakage Radar's affected sensor, and reports what is new: automations failing at run time (warn), entities automations use gone unavailable (warn), custom integrations breaking now or within the radar's window (warn) or later (info).

## 0.4.3

- `/api/snapshot.json` adds `stop_signatures`: per signature, the occurrences Core logged in the last 15 seconds before it exited (the s6 `Home Assistant Core finish process` line), such as a poll that races the closed HTTP session during a restart.

## 0.4.2

- Backup age counts backups that hold Home Assistant (full, or partial with Home Assistant); an add-on's own partial backup made it read fresh.
- Supervisor's `no_current_backup` is info: it counts full backups only, and Home Assistant's automatic backups are partial, so it stood as an action every night.

## 0.4.1

- An override is stored once: a run reads the last day, so a run between nightly ones (`POST /api/run`) stored the day's overrides again. Copies already stored count once in the ranking and `/api/overrides.json`.

## 0.4.0

- `harness_regressed` (warn): an Agent Harness Performance Tracker regression sensor that is on, found by the attributes that integration sets, with the gates that fired (version, window, the regressed tasks). Reported when new, and again when another gate fires.

## 0.3.2

- `kuma_ignore`: Uptime Kuma monitor names left out of the down check, for machines that are off unless a job needs them.

## 0.3.1

- Pending updates skip any whose title is an installed add-on's name; a local add-on's update entity has no hassio icon and was listed.
- The reference check fails when it reads no automation or script config, or finds no entity in any, so a silent run means it looked.

## 0.3.0

- Checks from the retired backups-VM HA health report, each reported when new since the previous run: safety-class devices and locks unavailable (action), batteries under 20% (warn), `update.*` entities other than core, OS, Supervisor and add-ons (info), and entity references in automation and script configs that name no live entity (warn).

## 0.2.0

- `GET /api/snapshot.json`: unavailable or unknown entities and ERROR/WARNING log signatures, read live. A signature is the level, logger and message with numbers, hex, IPs, MACs, UUIDs and quoted strings replaced, so a recurring error counts once. A failed read answers 502, never an empty snapshot.

## 0.1.2

- The Supervisor token is read back from the s6 container environment; without it every check read 401.

## 0.1.0

- Nightly checks: core and add-on updates, Supervisor issues, backup age, host disk, unavailable-entity delta, core log errors, Uptime Kuma monitors down.
- Override corpus from the logbook, with a per-automation ranking.
- Dark ingress page, JSON endpoints, phone summary.
