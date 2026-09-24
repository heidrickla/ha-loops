# Changelog

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
