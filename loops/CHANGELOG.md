# Changelog

## 0.3.2

- `kuma_ignore`: Uptime Kuma monitor names left out of the down check, for machines that are off
  unless a job needs them.

## 0.3.1

- Pending updates skip any whose title is an installed add-on's name; a local add-on's update entity
  has no hassio icon and was listed.
- The reference check fails when it reads no automation or script config, or finds no entity in any,
  so a silent run means it looked.

## 0.3.0

- Checks from the retired backups-VM HA health report, each reported when new since the previous run:
  safety-class devices and locks unavailable (action), batteries under 20% (warn), `update.*` entities
  other than core, OS, Supervisor and add-ons (info), and entity references in automation and script
  configs that name no live entity (warn).

## 0.2.0

- `GET /api/snapshot.json`: unavailable or unknown entities and ERROR/WARNING log signatures, read live. A
  signature is the level, logger and message with numbers, hex, IPs, MACs, UUIDs and quoted strings
  replaced, so a recurring error counts once. A failed read answers 502, never an empty snapshot.

## 0.1.2

- The Supervisor token is read back from the s6 container environment; without it every check read 401.

## 0.1.0

- Nightly checks: core and add-on updates, Supervisor issues, backup age, host disk, unavailable-entity delta, core log errors, Uptime Kuma monitors down.
- Override corpus from the logbook, with a per-automation ranking.
- Dark ingress page, JSON endpoints, phone summary.
