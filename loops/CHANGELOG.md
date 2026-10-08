# Changelog

## 0.7.1

- `energy_meter`: the panel's total energy statistic. An hour it did not move counts as missing for every circuit, since statistics keep writing zero-change rows while the meter is offline; a week of a dead meter read as a week of zero HVAC energy. Days measured without it are measured again.

## 0.7.0

- Loop 7, HVAC energy per cooling degree-day: `hvac_zones` and `outdoor_temperature` name long-term statistics; the nightly run fetches each local day's hourly energy change and mean temperature over Core's websocket, from `controller_from`, and keeps the inputs under `/data/controller`. A week's kWh per CDD65 per zone and for the house is a ratio of sums over at least five complete cooling days; a missing hour makes its zone-day unavailable. `controller_arm` labels each new day. Nothing changes a thermostat.
- Loop 4, the manual-action record: a websocket subscription keeps every change of an actionable entity and every change a person made, as canonical actions, per local day under `/data`, with the gaps when it was not connected or the add-on not running. The nightly run scores the no-action, most-frequent-action and hour-bucket policies on the later third of complete days, with false positives, provisional until 60 days.
- `/api/controller.json`, `/api/actions.json`, and an HVAC weeks table on the page.

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
