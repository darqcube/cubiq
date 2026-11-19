# Time zone: containers follow the host

Every Cubiq container reads the **time zone of the machine it runs on**, not UTC. Set the host's zone once and the
whole stack follows it:

```bash
sudo timedatectl set-timezone Europe/Paris    # any zone; UTC is fine too
timedatectl                                   # check it
```

Each service mounts the host's `/etc/localtime` and `/etc/timezone` read-only (see `compose/*.yaml`), so changing the
host's zone and recreating the containers (`make up`) is all it takes. `scripts/preflight.sh` checks that both files
exist: Docker makes a *directory* of a missing bind-mount source, which breaks a container's clock.

## What you will see

| | |
|---|---|
| Inside a container | `date` shows the host's local time |
| `make logs` | every service's timestamps in the host's zone, **including** the programs that always write UTC (below) |
| Grafana's own log | local time, with its offset (`+05:00`) |
| The portal and Grafana dashboards | your browser's zone, as before |

## What stays UTC, on purpose

- **Stored data.** Prometheus samples, Loki entries, Infrahub's records and the alert times are timestamps without a
  zone (Unix time) or tagged UTC. They never shift when a zone changes, so data from sites in different zones, or from
  before and after a daylight-saving change, stays comparable.
- **The programs that log UTC whatever the zone:** Prometheus, Alertmanager, Loki and Telegraf write `…Z` in their own
  logs. No setting changes that. `make logs` rewrites them on display (`scripts/local-time-logs.py`); `make logs UTC=1`
  shows them as written. Reading a log some other way (`docker logs`, a file shipper), expect UTC from those four.
- **APIs.** Timestamps in Prometheus', Alertmanager's and Cubiq's APIs are UTC or Unix time.

## Adding a service

A new service in `compose/*.yaml` needs the same two mounts; `automation/tests/test_time_zone.py` fails until it has them.
