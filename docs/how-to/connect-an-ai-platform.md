# Connect AgenticOps (or another AI platform)

Cubiq works standalone — Grafana, Prometheus, Loki and the automation API are
all usable by a person with no AI involved, and every feature is there without
a platform. An AI platform is an **additional** consumer, connected through three
seams: MCP tools in, alerts out, and (optionally) device changes.

**AgenticOps** is the AI platform Cubiq is built alongside: it adds what Cubiq
deliberately leaves out — people's sign-in, approvals and four eyes on every
change, alert policy (which alert starts an investigation), and Zero Trust.
Everything AgenticOps needs from Cubiq is **one optional YAML file,
`agenticops.yml`**; without it Cubiq runs standalone. Another platform can use
the same file and the same steps: nothing in it is specific to AgenticOps.

This guide connects the two **directly**: plain endpoints, tokens from the
platform's identity provider, on a network you trust. AgenticOps can instead
reach Cubiq through an Octelium Zero Trust overlay (no published ports, no
inbound connection to Cubiq); that way is set up from AgenticOps, and uses the
`overlay:` section of the same file ([below](#the-overlay-section)).

## 1. Tools in — the MCP servers

Seven servers, all reached over the Docker network:

| Server | Address | Gives the AI |
|---|---|---|
| `mcp-infrahub` | `http://mcp-infrahub:9001/mcp` | what should exist |
| `mcp-prometheus` | `http://mcp-prometheus:9002/mcp` | metrics, alerts, flows |
| `mcp-loki` | `http://mcp-loki:9003/mcp` | logs |
| `mcp-grafana` | `http://mcp-grafana:9004/mcp` | dashboards to link to |
| `mcp-netmiko` | `http://mcp-netmiko:9005/mcp` | device config and parsed state |
| `mcp-assurance` | `http://mcp-assurance:9006/mcp` | assurance checks, snapshots, parsed config |
| `mcp-pyats` | `http://mcp-pyats:9007/mcp` | Genie structured models — BGP neighbors, interfaces, LLDP, platform |

`mcp-infrahub` tools — fixed queries, each taking at most one validated name:

| Tool | Answers |
|---|---|
| `list_devices`, `get_device(device)`, `get_site_devices(site)` | which devices exist, where, and how they are reached |
| `list_applications` | every application with its criticality and the hosts it runs on |
| `get_application(application)` | its services (protocol, port), hosts, sites, owner |
| `get_host(host)` | address, site, subnet and gateway device, and the services it runs |
| `get_site_services(site)` | the hosts at a site and the applications they serve |
| `get_application_dependencies(application)` | the hosts, subnets, gateways and site devices an application depends on — "what breaks if X fails?" |

### Graphs in the chat (optional)

`ai_platform.graphs: true` in `site.yml` starts Grafana's image renderer (container `grafana-renderer`, profile
`graphs`) and registers two more tools on `mcp-grafana`:

| Tool | Answers |
|---|---|
| `render_interface_graph(device, interface, window, kind)` | a PNG of one interface's **traffic** or **errors** over the window, as a Markdown image link, plus the same numbers as text |
| `render_link_comparison(link_a, link_b, kind, window)` | two groups of links side by side, by interface description: **traffic_share** (two pies), or IP SLA **availability**, **rtt**, **jitter** (grouped bars per device) — [per-link probes](../devices/cisco-ios-xe.md#3b-ip-sla--wan-quality-and-application-response-time-optional) |

How the picture reaches the chat: chat platforms drop images inside tool
results, so the tool keeps the PNG in memory and returns
`![cr2 Et0/1 traffic, last 1h](http://192.0.2.10:9004/g/cr2-et0-1-traffic-1h-k7m2x9q4ab.png)`.
The model puts that line in its answer; the user's browser loads the image. The
link is its own authorisation, because an `<img>` cannot send a bearer token:
a readable label (models copy words reliably, and mangle long random strings)
plus a 50-bit random suffix, forgotten after 15 minutes (`MCP_IMAGE_TTL_SECONDS`).
After that, or after the server restarts (images are kept in memory only), the
link shows a small "Graph expired" image instead of a broken one. Ask again for
a fresh graph — and tell your agents to call `render_interface_graph` for every
graph request, never re-using a link from earlier in the chat.

- It renders panel 1 or 2 of the **Interface Detail** dashboard only, from
  validated arguments; a caller never names a dashboard, panel or query.
- Images come from `MCP_IMAGE_BASE_URL` (`http://<published address>:9004`).
  In production put an HTTPS reverse proxy in front and set that variable to
  it: platforms that enforce a Content-Security-Policy load `https:` images only.
- Tell the agent to paste the tool's `markdown` field verbatim — a model that
  rewrites the URL breaks the image (the `summary` and `dashboard_url` still work).
- Renders are headless Chromium: 2–8 s each, at most 4 at once, the container
  capped at 1.5 GB.

Apply on the Cubiq host: `python3 install.py --step 2 && make mcp-apply && make up`.

**Turning graphs off** (`graphs: false`; e.g. when the AI platform
draws its own charts from the metrics): the same three commands. `make mcp-apply`
unregisters both render tools, and `make up` stops and removes `grafana-renderer`:
it removes every service whose profile is no longer in `COMPOSE_PROFILES` (Compose
itself would leave it running). Grafana, its dashboards and `get_dashboard_link`
stay.

`mcp-pyats` tools — a feature is a name from the platform's `pyats:` allow-list
in `platforms.yml`, never a Genie command:

| Tool | Answers |
|---|---|
| `list_pyats_features(device)` | which Genie features this device's platform has (`learn` or `parse`) |
| `learn_device_feature(device, feature)` | Genie's structured model — `ok`, `absent` (not configured) or `error` |
| `get_bgp_neighbors(device)` | every BGP session as VRF, address family, peer and state |

The application tools return empty results until hosts and services are
modelled — [model-applications.md](model-applications.md). With services
modelled, `get_flow_summary` on `mcp-prometheus` also returns `by_application`.

Transport is streamable-HTTP; auth is a bearer credential:

```
Authorization: Bearer <credential>
```

### Who may call: credentials and scopes

Cubiq **authenticates**; it does not decide policy. Every caller gets one of two
scopes:

| Scope | Allows |
|---|---|
| `read` | every read tool |
| `execute` | also the change tools (stage, commit, rollback, withdraw), when writes are on |

| Credential | Scope | Where it comes from |
|---|---|---|
| `MCP_AUTH_TOKEN` (shared) | read | generated by `install.py`, in `.env` |
| `MCP_EXECUTE_TOKEN` (shared) | execute | generated by `install.py`, in `.env`; keep it for the automation that changes devices |
| A token from the platform's identity provider, issued to a client listed in `agenticops.yml` | that client's: `read` or `execute` | `access.clients` |
| Any other valid token from that provider (a person's, an agent's) | `access.default_scope`: `read`, or `none` | `access.default_scope` |

A token's **client** (`azp`) decides its scope — never a role, a group or a
person's name. Give `execute` to the platform's control service only (for
AgenticOps, Control's client): people and agents then read, and every change goes
through the platform, which approves it before it executes.

`MCP_AUTH_MODE` (`site.yml` `ai_platform.auth.mode`) decides which credentials
are accepted:

| Mode | Accepts | Default |
|---|---|---|
| `token` | the two shared tokens | without `agenticops.yml` |
| `oidc` | the provider's tokens only | with `agenticops.yml` `access:` |
| `both` | either — while moving, or to keep `make mcp-check` working with the token | – |

Every request is logged with who made it, its scope and the tool
(`docker compose logs mcp-netmiko | grep audit`):

```
audit user=alice sub=4f1c… via=oidc agent=platform-control scope=execute server=netmiko rpc=tools/call tool=stage_device_change
```

### If the platform runs in the same Compose project

Attach it to the `darqcube` network and use the service names above. Nothing to
publish.

### If it runs on another machine

By default the ports are published on `127.0.0.1` only. Two things connect a
platform on another host:

- **`site.yml`, Cubiq's own settings:** publish the MCP servers on an address
  the platform can reach (`ai_platform.publish`).
- **`agenticops.yml`, the connection:** who may call (the platform's identity
  provider and its clients) and where alerts also go. Its values live **on the
  platform's side**, so they are generated there.

Every value differs per deployment; the examples use documentation addresses
(`192.0.2.10` for Cubiq, `192.0.2.1` for the platform's host).

**1. On the platform's host**, from a clone of this repository:

```bash
python3 scripts/ai-platform-connect.py \
    --stack-host 192.0.2.10 \
    --idp http://localhost:7080/realms/<realm> \
    --audience cubiq \
    --client <control service's client id>=execute \
    --alert-webhook-url https://192.0.2.1/<platform's alert endpoint> \
    --token-header <header the platform forwards the caller's token in>
```

It reads the provider's OpenID configuration, works out the platform host's
address on the route to Cubiq, and writes:

| File | What it is | Goes to |
|---|---|---|
| `agenticops.yml` | `access:` (issuer, JWKS URL, audiences, clients and scopes) and `alerts.webhook`; no secrets | Cubiq, as `~/cubiq/agenticops.yml` |
| `mcp-servers.snippet.yaml` | the seven servers with their endpoints and credential source | the platform's MCP server configuration |

The issuer is kept exactly as tokens carry it (often `http://localhost:…`); only
the JWKS URL is rewritten to an address Cubiq can reach. Add
`--token-file user.jwt` to see which scope a real token would get. Repeat
`--client` for each machine client, e.g. a pre-production platform with
`=read`.

**2. Copy it to Cubiq** (gitignored: it names your addresses):

```bash
scp agenticops.yml <user>@192.0.2.10:~/cubiq/agenticops.yml
```

**3. On Cubiq**, publish the MCP servers in `site.yml`:

```yaml
ai_platform:
  publish:
    enabled: true
    bind_ip: ""        # empty: site.collector_ip
```

then apply:

```bash
cd ~/cubiq
python3 install.py --step 2      # reads site.yml and agenticops.yml into .env
make mcp-apply                   # recreate the MCP servers
docker compose up -d --force-recreate config-init alertmanager   # the platform's alert webhook
curl -s <jwks_url>               # Cubiq can fetch the provider's keys
```

Without a site file, `install.py --step 2` changes only the connection's keys in
the existing `.env` (and the auth mode, from `token` to `oidc`); publish by
setting `MCP_BIND_IP` and `MCP_ALLOWED_HOSTS` (`<ip>:*`) there.

**4. Register the servers** in the platform from `mcp-servers.snippet.yaml`,
then check from the platform's host:

```bash
python3 scripts/mcp-check.py --host 192.0.2.10 --token-file user.jwt
```

### Writing `agenticops.yml` by hand

The generator is a convenience; the file is plain YAML
([agenticops.example.yml](../../agenticops.example.yml)):

```yaml
access:
  issuer: https://idp.example/realms/main            # exactly the tokens' "iss"
  jwks_url: https://idp.example/realms/main/protocol/openid-connect/certs   # as reachable from Cubiq
  audiences: cubiq                                   # comma-separated; empty = not checked
  default_scope: read                                # any other valid token: read, or none
  clients:                                           # OIDC client id: read or execute
    platform-control: execute
    platform-preprod: read
  agent_claim: azp                                   # names the calling agent in the audit log
alerts:
  webhook: https://agenticops.example/internal/alerts
```

Every key is checked: a typo stops `install.py` instead of being ignored.
Remove the file (and run `python3 install.py --step 2`) to disconnect: Cubiq is
standalone again.

### The overlay section

`overlay.services` names what Cubiq serves through an overlay network, by name
and local port, for a platform that reaches Cubiq that way (AgenticOps with
Octelium). Direct mode, as in this guide, needs none of it.

```yaml
overlay:
  services:
    infrahub:          # a DNS label
      port: 9001
      protocol: http   # http or tcp
```

### TLS

Direct mode is plain HTTP, for a trusted network: there is no TLS between
components (see [INSTALL](../INSTALL.md)). Put a reverse proxy with a certificate
you provide in front of the published ports before exposing them any further.
(A built-in option is planned for a later Cubiq release.)

## 2. Alerts out — two webhooks

Every alert goes to two receivers, each a webhook POSTed as JSON, resolutions
included:

| Receiver | URL | Set in |
|---|---|---|
| `notify` | `ALERT_WEBHOOK_URL` | `site.yml` `alerts.webhook_url`: Cubiq's own (chat, mail relay) |
| `platform` | `PLATFORM_ALERT_WEBHOOK_URL` | `agenticops.yml` `alerts.webhook`: the connected platform's |

```bash
python3 install.py --step 2 && docker compose up -d --force-recreate config-init alertmanager
```

This is a push, so the platform learns about a problem without polling. Every
alert is sent; which ones start an investigation is the platform's decision (it
may read each device's `auto_investigate` attributes in Infrahub) —
[auto-investigate.md](auto-investigate.md). The URL may carry a path secret:
`agenticops.yml` is gitignored, and Alertmanager reads each URL from a file, so
its config never holds one.

## 3. Actions — the automation API

`http://automation:8100`, or `localhost:${AUTOMATION_PORT}` from the host. The
same endpoints `mcp-netmiko` and `mcp-assurance` front, if you prefer plain HTTP
to MCP.

---

## Letting automation change device configuration

### The chain, and what gates what

```
caller ──execute credential──> mcp-netmiko ──compose network──> automation API ──SSH──> devices
                                    ▲                              (loopback only)        ▲
                       MCP_ALLOW_WRITE: change tools exist?             device account privilege
                       the execute scope: may this caller change?       gates what ANY caller can do
```

### `MCP_ALLOW_WRITE` — whether the change tools exist

Off by default:

```bash
MCP_ALLOW_WRITE=false     # no change tool is REGISTERED
```

The tools are absent from `tools/list` entirely — not present and refusing.
A caller that cannot discover a tool cannot call it.

**Only Cubiq's owner switches it, on the Cubiq host**; `agenticops.yml` cannot.
To enable, and back:

```bash
make writes-on    # MCP_ALLOW_WRITE=true in .env, recreates mcp-netmiko, shows its tools
make writes-off   # read only again
```

With a site file, also set `ai_platform.allow_write` there: `install.py`
rebuilds `.env` from it (`make writes-on` reminds you when they differ).

```bash
make mcp-check
# netmiko: 2 tools = read only.  7 = changes on (stage, commit, rollback, withdraw, get).
```

### How a change runs

A caller with the execute scope **stages** a change (nothing is sent; the result
has its fingerprint and which lines the device already holds), then **commits**
it once with that fingerprint and an idempotency key. A retry with the same key
pushes nothing; a device whose configuration changed since the stage is refused
before anything is sent; the running config is archived first; every action is
in the execution log. [run-changes.md](run-changes.md)

**Approval is the platform's.** Cubiq has no approval step: with AgenticOps,
Control holds the only execute client and commits only what a person approved
(with four eyes, windows and scopes as AgenticOps is configured). A platform
without an approval concept should not hold an execute credential.

### The automation API underneath

It has no authentication of its own and trusts the caller mcp-netmiko verified.
So it is published on **loopback only** (`AUTOMATION_BIND_IP=127.0.0.1`, the
default). This costs the platform nothing: the MCP servers reach it over the
Docker network. From the host, `make change-stage` and `make change-commit` use
it directly.

### The stronger control: the device account

`MCP_ALLOW_WRITE` is configuration you own. The device account privilege is
enforced **by the device**, which is why it survives a misconfiguration:

```
! Cisco IOS-XE
username darqcube privilege 5 secret <PASSWORD>      ! read-only
```

With a read-only account Cubiq physically cannot change anything, whatever the
flag says or who reaches the API. You keep every metric, log, parsed state and
assurance check. See
[deploy-on-customer-premises.md](deploy-on-customer-premises.md#3-start-with-a-read-only-device-account).

### Which combination to use

| Scenario | `MCP_ALLOW_WRITE` | Execute credential | Device account |
|---|---|---|---|
| Demo on your own machine | either | either | either |
| PoC at a customer, read-only | `false` | none | **read-only** |
| AI reads, people change | `false` | none | privileged |
| AgenticOps reads and changes | `true` | **Control's client only** (`agenticops.yml`) | privileged |
| Standalone automation changes | `true` | `MCP_EXECUTE_TOKEN`, held by that automation | privileged |

---

## When you connect a production AI platform

- **Identity.** With `agenticops.yml` each call carries a token from the
  platform's provider, and the audit log names the person and the agent. In
  `token` mode the log can only say `token` or `execute-token`.
- **Per-tool authorisation.** Cubiq has two scopes. Finer control — this agent
  may read logs, that one may run checks — belongs in the platform, or in a
  gateway in front of the servers.
- **Approval on writes.** None in Cubiq, by design: it is the platform's. Give
  execute only to a client that enforces it.
- **Rate limiting.** Nothing stops an agent looping over 400 devices. The
  per-device lock serialises one device; it does not bound overall volume.

None of these block a read-only integration, which is where most platforms
start.

## Moving from 1.x

1.x kept the connection in `sites/ai-platform.yml`, merged over `site.yml`, and
checked people's roles and approvals itself. In 2.0.0:

| 1.x | 2.0.0 |
|---|---|
| `sites/ai-platform.yml` | `agenticops.yml` (`install.py` stops on the old file, saying so) |
| `ai_platform.auth.issuer`, `jwks_url`, `audiences`, `agent_claim` | `agenticops.yml` `access:` |
| `ai_platform.alert_webhook_url` | `agenticops.yml` `alerts.webhook` (a second receiver; `alerts.webhook_url` stays Cubiq's own) |
| `ai_platform.alert_profile` | `site.yml` `alerts.profile` |
| roles (`write_role`, `propose_role`, …), `approvals_audience`, `fresh_signin_claim`, `changes:` | the platform's policy; Cubiq checks the execute scope ([run-changes.md](run-changes.md#moving-from-1x)) |
| the `investigate` receiver | every alert to `notify` and `platform` ([auto-investigate.md](auto-investigate.md#moving-from-1x)) |
| `ai-platform-connect.py --auth-mode`, `--bind-ip`, `--graphs`, `--max-response-kb` | `site.yml` (the script names each) |

## What the tools deliberately do not do

No tool takes a raw PromQL, LogQL, GraphQL or CLI string. Every query is
composed server-side from validated arguments. This is not a limitation to work
around — one passthrough tool makes every other boundary in the stack
decorative, and it is very hard to remove once something depends on it.

If a question cannot be answered by the existing tools, add a **specific** tool
for it in `mcp/servers/<server>.py` rather than a general one.

## Check the surface

```bash
make mcp-check
```

A real MCP handshake against every server, listing its tools.
