# MCP servers

## What they do here

Expose the rest of the stack to an AI platform as bounded tools. Seven containers
from **one image**; `MCP_SERVER` picks the module, `PORT` picks the port.

**Optional by design.** Drop `mcp` from `COMPOSE_PROFILES` and nothing else
loses a feature — nothing in the stack depends on them.

| Server | Port | Backend | Tools |
|---|---|---|---|
| `mcp-infrahub` | 9001 | Infrahub | 9 |
| `mcp-prometheus` | 9002 | Prometheus (and Alertmanager's silences) | 8 |
| `mcp-loki` | 9003 | Loki | 3 |
| `mcp-grafana` | 9004 | Grafana | 2 (3 with graphs on) |
| `mcp-netmiko` | 9005 | automation API | 2 (3 with writes on) |
| `mcp-assurance` | 9006 | automation API | 4 |
| `mcp-pyats` | 9007 | automation API (pyATS/Genie) | 3 |

## Configuration

| Where | What |
|---|---|
| `mcp/common.py` | transport, auth, argument bounds |
| `mcp/servers/*.py` | one module per server |
| `mcp/serve.py` | entry point |
| `.env` → `MCP_AUTH_TOKEN` | shared bearer token, all seven: the **read** scope |
| `.env` → `MCP_EXECUTE_TOKEN` | shared bearer token with the **execute** scope (the change tools) |
| `.env` → `MCP_AUTH_MODE` | `token` (the shared tokens), `oidc` (each caller's own JWT), or `both` |
| `.env` → `MCP_OIDC_*` | the identity provider and each client's scope, from `agenticops.yml` |
| `.env` → `MCP_ALLOW_WRITE` | whether the change tools exist |
| `.env` → `MCP_BIND_IP` / `MCP_ALLOWED_HOSTS` | where the ports are published, and the Host headers answered |
| `.env` → `MCP_MAX_RESPONSE_BYTES` | cap on one tool result — lower it for small local models |

## This host only, unless published

All seven are reached over the Docker network (`http://mcp-prometheus:9002/mcp`)
and published on `MCP_BIND_IP`, which defaults to `127.0.0.1` — this host only.
An ai-platform on another machine needs them published on this host's network
address, and the servers told to answer to it: both come from
`ai_platform.publish` in `site.yml`; who may call comes from `agenticops.yml` —
[../how-to/connect-an-ai-platform.md](../how-to/connect-an-ai-platform.md).

The MCP SDK refuses any request whose `Host` header is not on the server's
allow-list, with **421**. Loopback and the compose names are always on it;
`MCP_ALLOWED_HOSTS` adds the published address.

## No passthrough tools

No tool takes a raw PromQL, LogQL, GraphQL or CLI string. Every query is
composed server-side from validated arguments.

This is not conservatism. One passthrough tool makes every other boundary in the
stack decorative, and it is very hard to remove once something depends on it. If
a question cannot be answered by the existing tools, add a **specific** tool for
it.

A test enforces this by inspecting every tool signature.

## The change tools

`stage_device_change`, `commit_device_change`, `rollback_device_change` and
`withdraw_device_change` on `mcp-netmiko` are the only tools that change
anything.

```bash
MCP_ALLOW_WRITE=false     # default — the tools are NOT REGISTERED
```

With the default they are absent from `tools/list` entirely. Not "present but
refuse" — absent, so a caller that does not know about them cannot discover
them. Turning automation loose on device configuration should be a deliberate
act, not a side effect of a container starting.

```bash
make writes-on            # MCP_ALLOW_WRITE=true, recreates mcp-netmiko
```

The second gate is the **execute scope**: the execute token, or a client
`agenticops.yml` gives execute. Every other caller is refused by every change
tool, whatever roles its token carries. A change is staged (nothing sent), then
committed once with its fingerprint and an idempotency key; the running config
is archived first, and the size is capped by `MAX_CONFIG_LINES`:
[../how-to/run-changes.md](../how-to/run-changes.md). The automation API
underneath has no authentication of its own, so it is published on loopback
only (`AUTOMATION_BIND_IP`).

**Device groups** (`mcp-infrahub`, read): `list_device_groups` lists Infrahub's
standard groups and the devices in each (made in Infrahub's UI, or by `make seed`
from a `groups:` file); `get_device` gives a device's own `groups`. A connected
platform uses them to know which team owns which devices. Infrahub's internal
groups are left out, and only devices are listed as members.

**Silences** (`mcp-prometheus`): `list_silences` (read) shows the silences in
force; `silence_device` quiets one device's alerts at Alertmanager for 1 to 168
hours, so no receiver is notified (maintenance), and `end_silence` ends one early.
Making or ending a silence needs the execute scope, checked before Alertmanager is
called. Only by the `device` label, never a regex; the reason and who asked are
kept with the silence. A connected platform decides who may ask (AgenticOps: an
incident resolved as Maintenance); Cubiq only that the caller holds execute.

## Verify

```bash
make mcp-check
```

A real client handshake against each server — initialize, then `tools/list` —
not just `/healthz`, which answers even when every MCP call is refused. With
writes off, netmiko lists 2 tools; with writes on, 7.

```bash
.venv/bin/python -m pytest automation/tests/test_mcp.py automation/tests/test_mcp_transport.py -v
```

## Problems

| Symptom | Cause |
|---|---|
| 401 on every call | missing or wrong bearer credential — the shared token in `token` mode, a valid JWT from the configured issuer in `oidc` mode (`docker compose logs mcp-netmiko` says why) |
| 421 Misdirected Request | the `Host` the client used is not allowed — set `ai_platform.publish` so `MCP_ALLOWED_HOSTS` names this host's address |
| 406 / stream errors | add `Accept: application/json, text/event-stream` |
| A server exits at start | `MCP_SERVER` is not one of the seven valid names, `MCP_AUTH_TOKEN` is empty, or `oidc` mode has no platform (`MCP_OIDC_PLATFORMS` empty: `agenticops.yml` `platforms:`, then `python3 install.py --step 2`) |
| `mcp-netmiko` / `mcp-pyats` tools fail | the automation container is down — they front its API |
| a change tool says the caller "needs execute" | the credential has the read scope: the read token, or a client not listed with `execute` in `agenticops.yml` — intended |
| the change tools missing | `MCP_ALLOW_WRITE` is not `true` (this is the default, and intended) |
