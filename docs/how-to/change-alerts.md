# Add or change an alert

Alerts live in `observability/prometheus/rules/alerts.yml`. It is plain
Prometheus rule YAML — no templating, no generation.

## Add one

```yaml
groups:
  - name: network-devices          # add to an existing group, or make a new one
    rules:
      - alert: InterfaceFlapping
        expr: changes(interface_oper_status[15m]) > 6
        for: 5m                     # must be true this long before firing
        labels:
          severity: warning         # warning | critical
        annotations:
          summary: "{{ $labels.device }} {{ $labels.ifName }} is flapping"
          description: "{{ $value }} state changes in 15 minutes"
```

Check and apply:

```bash
docker run --rm -v "$PWD/observability/prometheus:/p:ro" --entrypoint promtool \
  prom/prometheus:v3.4.1 check rules /p/rules/alerts.yml

curl -X POST localhost:${PROMETHEUS_PORT:-9090}/-/reload
```

`--web.enable-lifecycle` is on, so a reload needs no restart.

## The metrics you can alert on

| Metric | Meaning |
|---|---|
| `interface_oper_status` | 1 = up, 2 = down (IF-MIB) |
| `interface_admin_status` | 1 = up, 2 = shut |
| `interface_in_octets` / `_out_octets` | 64-bit counters — use `rate()` |
| `interface_in_errors` / `_out_errors` | error counters |
| `interface_speed` | Mbit/s |
| `cpu_usage` | percent, one series per core/entity |
| `memory_used` / `_free` / `_total` | raw, units vary — see below |
| `device_uptime` | SNMP sysUpTime |
| `bgp_peer_state` | BGP4-MIB, 6 = established; with `bgp_peer_admin_status` (2 = start) |
| `intent_device_present` | 1 per device active in Infrahub (from `make render`) |
| `device:not_reporting` | recording rule — intended devices with no SNMP/gNMI data |
| `link:info` / `link:end:up` | network map — one series per link (`kind` lldp or bgp); `link:end:up` 1 = interface up (lldp) or session established (bgp) |
| `intent_link_present` | 1 per end of each active cable in Infrahub (from `make render`); labels `device`, `ifName`, `neighbor`, `neighbor_port`, `cable` |
| `cable:end:up` | recording rule — each intended cable end, 1 = its port up, 0 = down or not reporting; never ages out (LLDP's links forget after 30 min) |
| `assurance_rule_state` | scheduled assurance — `1` per device and rule, label `state` = pass / fail / error / skipped |
| `netflow_flow_bytes_total` / `netflow_flow_packets_total` | NetFlow/IPFIX — **bytes (packets) per 60 s window, a gauge**, one sample per window and none for a minute without flows (totals: `sum_over_time`); labels `protocol`, `direction`, `application`, `criticality` |
| `device:cpu_usage:max` | recording rule — one value per device |
| `device:memory_used_percent:max` | recording rule — comparable across vendors |

Every metric carries `device`, `site`, `role` and `platform` from Infrahub, plus
`ifName`, `ifAlias` and `ifType` on interface metrics.

**Alert on `device:memory_used_percent:max`, not on `memory_used`.** The three
vendors report memory in percent, bytes and allocation units respectively; the
recording rule in `observability/prometheus/rules/recording.yml` is what makes them comparable.

**Intent and routing alerts shipped:** `DeviceNotReporting` (active in
Infrahub, no SNMP/gNMI data for 10 min — names the device and site) and
`BGPPeerDown` (a peer configured up that is not Established for 5 min) and
`OSPFNeighborDown` (an adjacency neither full nor two-way, or gone from the
router's table, for 2 min).

**WAN quality alerts shipped:** `IPSLAProbeFailing` (a probe below 80 %
success over 15 min) and `WANLatencyHigh` (ICMP probe RTT above 150 ms for
10 min — set the threshold to the WAN's SLA).

**Assurance alerts shipped** (need `ASSURANCE_INTERVAL_MINUTES` > 0, or an
on-demand check): `AssuranceCheckFailing` (a rule in `fail` or `error` for
20 min — longer than one interval, so a single bad run does not page) and
`AssuranceStale` (a device not assured for three intervals — the scheduler
stopped or the device keeps timing out). An on-demand check (`make check`,
the API, `run_device_checks`) updates the same state, so a check that passes
after a fix clears `AssuranceCheckFailing` at once.

**Alert profiles: how fast alerts react.** `IPSLAProbeFailing` and
`AssuranceCheckFailing` come in two timings, in
`observability/prometheus/profiles/`. Exactly one is loaded:

| Profile | IP SLA judged over | Assurance fires after | For |
|---|---|---|---|
| `standard` (default) | 15 min | 20 min | production: few false alarms |
| `demo` | 5 min | 2 min | drills and demonstrations: alerts fire and clear within minutes |

Set `alerts.profile` in `site.yml` (or `ai_platform.alert_profile` in an
ai-platform overlay), then `python3 install.py --step 2 && make up`; changing
it recreates Prometheus. Edit both files together: a test checks they define
the same alerts, and promtool tests (`observability/prometheus/tests/profile-*.test.yml`)
check each one's timing.

**Flow alerts shipped:** `NoFlowsReceived` (no flow from anywhere for 15 min)
and `CriticalApplicationSilent` (a `critical` application that had traffic in
the last 6 h has had none for 15 min — needs services modelled in Infrahub).

## Useful patterns

```promql
# Admin-up but oper-down — ignores deliberately shut ports
interface_oper_status == 2 and interface_admin_status == 1

# Physical ports only (ifType 6), excluding tunnels/SVIs/loopbacks
interface_oper_status{ifType="6"} == 2

# A device that was being polled and has stopped
absent_over_time(device_uptime{device="router1"}[10m])

# Only alert on core devices
device:cpu_usage:max{role="core"} > 85
```

## Sending alerts somewhere

Every alert goes to two receivers, each a webhook:

| Receiver | URL | Set in |
|---|---|---|
| `notify` | `ALERT_WEBHOOK_URL` | `site.yml` `alerts.webhook_url` |
| `platform` | one webhook per connected platform (`PLATFORM_ALERT_WEBHOOKS`), each with its token | `agenticops.yml` `platforms.<name>.alert_webhook`; the token: `install.py --platform-token <name>` |

```bash
python3 install.py --step 2 && docker compose up -d --force-recreate config-init alertmanager
```

Either left empty delivers nowhere: alerts collect in the Alertmanager UI and the
stack starts with no external dependency (with no platform connected, the
`platform` receiver has no webhook at all). The body's `"receiver"` says which
receiver a delivery is for. The stack does not decide which alert matters to
whom: a connected platform does, from what it receives (it may read each
device's `auto_investigate` attributes in Infrahub: [auto-investigate.md](auto-investigate.md)).

**Alertmanager has no environment-variable expansion of its own**, so
`config-init` writes each URL to a file (`/etc/alertmanager/webhook-url`, `…/platform-<name>-url`, and each platform's
token to `…/platform-<name>-token`) that its receiver reads (`url_file`, `credentials_file`), the platforms' entries to
`observability/telegraf/generated/alertmanager-platforms.yml`, and the config itself from
`observability/alertmanager/alertmanager.yml.tmpl` with those entries inserted, on every start (`make render` too,
and reloads Alertmanager). Edit the `.tmpl`, never the written copy, then `make render`.

For Slack, email or PagerDuty, add it to a receiver in the template:

```yaml
receivers:
  - name: notify
    webhook_configs:
      - url_file: /etc/alertmanager/webhook-url
        send_resolved: true
    slack_configs:
      - api_url: https://hooks.slack.com/services/XXX
        channel: "#network"
```

## Silencing

Use the Alertmanager UI at `localhost:${ALERTMANAGER_PORT}` → Silences, or set a
device's status to `maintenance` in `devices.yml` and re-run `make seed &&
make render` to stop polling it entirely.
