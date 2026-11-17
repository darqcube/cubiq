# Grafana and Alertmanager

## Grafana

Dashboards over Prometheus and Loki. The only user-facing component.

Without it: collection continues and alerts still fire — you just cannot see
graphs.

### Configuration

| Where | What |
|---|---|
| `observability/grafana/provisioning/datasources/datasources.yml` | Prometheus and Loki |
| `observability/grafana/provisioning/dashboards/provider.yml` | where dashboards load from |
| `observability/grafana/provisioning/dashboards/darqcube/` | the dashboards |
| `.env` → `GRAFANA_ADMIN_USER` / `_PASSWORD` | login |
| `.env` → `GRAFANA_PORT` | published port (default 13000) |

### Plugins

The Network Map dashboard uses Network Weathermap NG. Grafana installs
`GRAFANA_PLUGINS` (`.env`) before it starts serving; air-gapped installs:
[../how-to/change-dashboards.md](../how-to/change-dashboards.md).

### Dashboards are read-only in the UI

`allowUiUpdates: false`, deliberately: what is in the repo is what is deployed.
To change one, edit it in the UI, copy the JSON model, and paste it over the
file — [../how-to/change-dashboards.md](../how-to/change-dashboards.md).

### Verify

```bash
source .env
curl -sS "localhost:${GRAFANA_PORT}/api/health"
curl -sS -u "admin:${GRAFANA_ADMIN_PASSWORD}" "localhost:${GRAFANA_PORT}/api/datasources" \
  | .venv/bin/python -c "import json,sys;[print(d['name'],d['uid']) for d in json.load(sys.stdin)]"
```

Datasource *health* is the check that matters — a datasource can exist and not
connect:

```bash
curl -sS -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
  "localhost:${GRAFANA_PORT}/api/datasources/uid/darqcube-prometheus/health"
```

---

## Alertmanager

Receives alerts from Prometheus, groups and deduplicates them, delivers to one
webhook.

Without it: alerts still *evaluate* in Prometheus and are visible there — they
just are not delivered anywhere.

### Configuration

| Where | What |
|---|---|
| `observability/alertmanager/alertmanager.yml.tmpl` | **the template — edit this** |
| `.env` → `ALERT_WEBHOOK_URL` | where alerts go |
| `.env` → `ALERTMANAGER_PORT` | published port (default 19093) |

### Why it is a template

**Alertmanager has no environment-variable expansion at all** — a `${VAR}` in
its config is a hard parse failure. The `config-init` service writes each URL to a
file its receiver reads (`url_file`): `notify` (`ALERT_WEBHOOK_URL`) and `platform`
(one webhook per connected platform, `PLATFORM_ALERT_WEBHOOKS`, each platform's
token in a file too: `credentials_file`). It also writes the config from the
template on every start; `make render` does too, and reloads Alertmanager.
Every alert goes to both receivers ([change-alerts.md](../how-to/change-alerts.md#sending-alerts-somewhere)).

Edit the `.tmpl`, never the rendered copy, then:

```bash
docker compose up -d --force-recreate config-init alertmanager
```

### Grouping and inhibition

Alerts group by `alertname` and `device`. One inhibit rule matters: a
`DeviceUnreachable` alert suppresses `InterfaceDown` for the same device — you
get the cause, not a hundred symptoms.

### Alerts in seconds (syslog)

A router logs a link or neighbour going down the second it happens; polling
notices it a poll later and its rule waits (`for`) so a flap pages nobody:
about 2¼ minutes in all. So Logstash also raises `InterfaceDown`,
`BGPPeerDown` and `OSPFNeighborDown` from the line itself
(`%LINEPROTO-5-UPDOWN`, `%BGP-5-ADJCHANGE`, `%OSPF-5-ADJCHG`), labelled
`source=syslog`, straight to Alertmanager's API; the "up" line resolves it.
Their route sends them to the platforms at once (`group_wait: 0s`, one group
per link or neighbour). The polled alerts stay, as confirmation and for what
syslog misses. Nothing is raised for an interface an administrator shut, or a
BGP neighbour shut or deleted in the configuration. Whether to act on the
first signal or wait for a flap is the receiving platform's policy.

Check it: shut a link's far end and watch Alertmanager
(`curl -s localhost:9093/api/v2/alerts | grep syslog`) within a second or two.

### Verify

```bash
source .env
curl -sS "localhost:${ALERTMANAGER_PORT}/-/ready"
curl -sS "localhost:${ALERTMANAGER_PORT}/api/v2/alerts"
docker compose logs config-init          # says where alerts will be delivered
```

### Problems

| Symptom | Cause |
|---|---|
| Will not start, config parse error | a `${VAR}` left in the template — it is not expanded |
| Alerts fire but arrive nowhere | `ALERT_WEBHOOK_URL` empty (and no platform connected): the receiver then has no webhook at all, by design; set it and `docker compose up -d --force-recreate config-init alertmanager` |
| Changed the URL, no effect | recreate `config-init`, not just `alertmanager` |
