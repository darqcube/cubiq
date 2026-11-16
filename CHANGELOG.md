# Changelog

Versions are git tags `vMAJOR.MINOR.PATCH` on `main`, newest first here.

- **MAJOR**: something breaks for whoever deploys or calls Cubiq (an API, an MCP tool, a setting or the site file).
- **MINOR**: new features, backward compatible.
- **PATCH**: fixes only.

A platform that depends on a feature names the version it needs (for example: the execute scope needs 2.0.0 or later).

## 2.0.0 (unreleased)

**The stack is now Cubiq**, and it **authenticates; it no longer decides policy.** Cubiq is complete standalone; a
platform (AgenticOps, or another of the same shape) connects through one optional file. Approval, change rights, change
windows, four eyes, management interfaces and alert routing are the connected platform's. Breaking: read
*Upgrading* before you move a 1.x install.

### Breaking
- **The connection is `agenticops.yml`** (optional, gitignored, beside `site.yml`; `agenticops.example.yml`):
  `platforms:`, one entry per connected platform by a name you choose (its identity provider, `default_scope` read or
  none, each client's scope: `clients: {id: read|execute}`, and its `alert_webhook`), and `overlay.services`. Absent:
  standalone. Several platforms at once (a production platform and its staging copy): a token's issuer picks the
  platform, whose keys check it and whose clients scope it; two platforms never share an issuer. It replaces `sites/ai-platform.yml` and
  `ai_platform.auth.issuer`, `jwks_url`, `audiences`, `agent_claim`, `ai_platform.alert_webhook_url`; `install.py --ai-platform`
  becomes `--agenticops`. `ai_platform.alert_profile` moves to `alerts.profile`. Publishing, graphs and the result cap
  stay in `site.yml`.
- **Scopes, not roles.** A caller has the `read` or the `execute` scope. Token mode has two secrets: `MCP_AUTH_TOKEN`
  (read) and the new `MCP_EXECUTE_TOKEN` (execute, generated; must differ). With an identity provider the token's client
  (`azp`) decides, from its platform's `clients`; any other valid token gets that platform's `default_scope` (read or
  none, never execute). A person's roles count for nothing. `MCP_OIDC_PLATFORMS` (JSON, written by `install.py`)
  replaces `MCP_OIDC_ISSUER`, `…_JWKS_URL`, `…_AUDIENCES`, `…_DEFAULT_SCOPE`, `…_CLIENT_SCOPES` and `MCP_AGENT_CLAIM`.
  The audit line names the platform and the scope; a change records the platform that staged it (`requester_platform`).
- **Changes: stage, then commit once.** `stage_device_change` reads the device's configuration and returns the
  fingerprint (lines, rollback and that configuration) and `already_present` (the dry run); nothing is sent.
  `commit_device_change(change_id, fingerprint, idempotency_key)` pushes once: a retry with the same key returns the
  first result, another key is refused (409), and a device whose configuration changed since the stage (comments,
  timestamps and `ntp clock-period` ignored) is refused before anything is sent. `rollback_device_change(change_id,
  idempotency_key)` once. Every change tool needs execute; `get_device_change` read. States: staged, committing,
  committed, withdrawn, failed.
- **Gone, to the connected platform:** the approvals path (`/changes…` on mcp-netmiko for people, the approvals
  audience), the change rights (`propose`, `approve`, `commit`, `commit any` roles), the windows, fresh sign-in, four
  eyes, management interfaces (`CHANGE_MANAGEMENT_INTERFACES`, the `management:` section of `protected.yml`),
  `WRITE_MODE=direct` with `push_device_config` and `POST /device/{name}/config`, `make config-put`, and the
  `ChangeWaitingForApproval` alert. `site.yml`'s `changes:` section and role keys stop the install, saying so.
- **Every alert to both receivers.** Alertmanager has `notify` (`ALERT_WEBHOOK_URL`, Cubiq's own) and `platform`
  (one webhook per connected platform, `PLATFORM_ALERT_WEBHOOKS`, from `agenticops.yml`), each sending that
  platform's token as `Authorization: Bearer` (`PLATFORM_ALERT_TOKENS`, set with `install.py --platform-token NAME` from
  standard input: never in YAML, never printed); with no platform connected the receiver has no webhook and sends
  nothing. The same for Cubiq's own receiver with `ALERT_WEBHOOK_URL` empty: no webhook (it used to post to
  Alertmanager's `/-/ready`, which answers 405, so every alert group logged a failed delivery), and the URL is read
  from config-init's environment, no longer pasted into its command. The `investigate` receiver and the routes built from `auto_investigate` are gone. The three `auto_investigate` attributes stay in Infrahub as data a platform reads.
  config-init writes Alertmanager's config from its template on every start.
- **`docs/how-to/approve-changes.md` is `run-changes.md`.**

### Features
- `make change-stage DEV=… FILE=…` and `make change-commit ID=… FP=…`: stage and commit from the Cubiq host.
- `scripts/ai-platform-connect.py` writes `agenticops.yml` (`--name`, `--client ID=SCOPE`, `--default-scope`;
  `--merge FILE` adds a platform to an existing file, keeping the others; an explicit `--jwks-host` always wins), and
  names where each 1.x option went.
- `docs/how-to/connect-an-ai-platform.md` is the guide for connecting AgenticOps directly, without Octelium.

### Unchanged on purpose
- The name "Cubiq" is prose only: the compose project (`darqcube`), images, labels, volumes, metrics, env-var and tool
  names stay, so an upgrade keeps every container and its data. Grafana's folder is now shown as **Cubiq** (same
  dashboards, same uids; an empty **DarqCube** folder may remain and can be deleted).
- Protected lines (`reload`, `write erase`, removing a routing process, users and AAA) are still refused at staging.

### Upgrading from 1.5.x
1. **Close what is in flight:** no change may be waiting or approved (withdraw them). One left over is closed when the
   automation API starts ("closed by the move to 2.0.0"): stage it again.
2. **`git fetch --tags && git checkout v2.0.0`.** The repository is `darqcube/cubiq` on GitHub (the old URL redirects).
3. **Move the connection:** generate `agenticops.yml` on the platform's host (`scripts/ai-platform-connect.py …
   --client <control client>=execute`), copy it beside `site.yml`, and delete `sites/ai-platform.yml`.
4. **Edit `site.yml`:** remove the `changes:` section and the `ai_platform.auth` keys other than `mode`; move
   `ai_platform.alert_profile` to `alerts.profile`. `python3 install.py --step 2` names anything left.
5. **Apply:** `python3 install.py --step 2` (it also generates `MCP_EXECUTE_TOKEN`), then `make up` and `make mcp-apply`.
6. **The platform:** it must use the new change tools (fingerprint, idempotency key) and decide from every alert
   itself; AgenticOps 2.0.0 does. Device usernames, Infrahub data and metrics need nothing.

## 1.5.1 (2026-11-05)

### Fixes
- `make render` no longer deletes its Telegraf files before writing them again: a file written again is overwritten in
  place, and only what the run no longer writes is removed, at the end. Telegraf watches the directory and once reloaded
  while `intent.conf` was gone, dropping `intent_device_present` (and 1.5.0's `intent_link_present`) until it restarted.
  After upgrading from 1.5.0 with that symptom: `docker compose restart telegraf`, once.

## 1.5.0 (2026-11-05)

### Features
- **Cables in Infrahub**: a new `NetworkCable` node (two devices, their ports as the devices name them, a status),
  seeded from a `cables:` section like every other kind (`source-of-truth/devices/examples/cables.yml`). A port carries
  one cable at a time; `make seed` refuses a second.
- **`intent_link_present`**: `make render` writes one series per end of each active cable, beside
  `intent_device_present` (labels `device`, `ifName`, `neighbor`, `neighbor_port`, `cable`).
- **`cable:end:up`**: each intended cable end's state from its own port (1 up, 0 down or not reporting), with
  `network:cables_intended:count` and `network:cables_down:count`. Unlike LLDP's links it never ages out, and it covers
  ports with no LLDP neighbour.

### After upgrading
- `make schema`, add your cables (`cp source-of-truth/devices/examples/cables.yml source-of-truth/devices/`, edit),
  `make seed`, `make render`, and reload Prometheus's rules.

## 1.4.2 (2026-11-03)

### Fixes
- `OSPFNeighborDown` no longer fires when the router stops answering SNMP: an adjacency counts as gone only while the
  router still reports its interfaces. A 4-minute polling gap raised it for every adjacency at once; a silent router is
  `DeviceNotReporting`'s. The "OSPF sessions" table on Network Overview follows the same rule.

## 1.4.1 (2026-11-02)

### Fixes
- `make render` recreates infrahub-server first when `platforms.yml` changed since it started: the file is a single-file
  mount, so after `git checkout` of a release the container still read the old one, and the render silently left out
  what the release added (1.4.0's OSPF poll on the first VM that upgraded).

## 1.4.0 (2026-11-02)

### Features
- **OSPF adjacencies**: platforms with `ospf: true` (IOS-XE, verified) poll OSPF-MIB's neighbour table
  (`ospf_neighbor_state`, tags `neighbor`, `router_id`). `ospf:neighbor:down` counts an adjacency neither full nor
  two-way, or gone from the router's table in the last hour; **`OSPFNeighborDown`** fires after 2 minutes.
- **Network Overview**: "OSPF sessions down" and an "OSPF sessions" table (every adjacency, its state, worst first).
- **Auto-investigate**: `ospf` in a device's `auto_investigate_protocols` now routes `OSPFNeighborDown` to
  `investigate`.

### Changes
- A decided change's `ChangeWaitingForApproval` is reported resolved within about a minute (`group_interval: 1m` on its
  route), not at the 5-minute group interval.
- After upgrading: `make render` (the OSPF poll and the Alertmanager config), and reload Prometheus's rules.

## 1.3.0 (2026-11-02)

### Changes
- **`ChangeWaitingForApproval` is one alert per waiting change** (from `change_state`), labelled `change_id`, and
  Alertmanager groups it per change: a change that starts waiting while another waits is sent on its own within
  `group_wait`, instead of waiting for the first one's group. Its summary names the change. Before, one alert counted all
  waiting changes (`sum(changes{state="waiting"})`).

## 1.2.0 (2026-11-02)

### Features
- **`change_state{change_id, state}`** on the automation API's `/metrics`: each change staged in the last 24 hours with
  its current state (only the id and the state). A platform shows the person who asked for a change that it was approved
  without a sign-in to the approvals path. `docs/how-to/approve-changes.md`, "Following one change".
- **`--fresh-signin-claim auth_time|iat`** on `scripts/ai-platform-connect.py`: the overlay carries the sign-in claim, so
  re-applying it no longer puts `auth_time` back on a platform that needs `iat` (every approval was refused as not fresh).

## 1.1.1 (2026-11-02)

### Fixes
- Alertmanager did not start on 1.1.0: its generated config was mounted inside its read-only `/etc/alertmanager`
  volume. It is now mounted at `/etc/alertmanager-generated`; a test refuses any volume mounted inside another.

## 1.1.0 (2026-11-02)

### Features
- **Auto-investigate policy in Infrahub**: three Device attributes (`auto_investigate`, off by default;
  `auto_investigate_interfaces`, `ifName` patterns; `auto_investigate_protocols`) decide which alerts start an AI
  investigation. `make render` turns them into Alertmanager routes to a new `investigate` receiver and reloads
  Alertmanager; every other alert goes to `notify`. Both receivers use the one `ALERT_WEBHOOK_URL`; the body's
  `"receiver"` names the path. BGP is watched today; OSPF, IS-IS, EIGRP and RIP are accepted and reported as not
  watched yet. `docs/how-to/auto-investigate.md`.

### Changes
- Alertmanager reads its config from `observability/telegraf/generated/alertmanager.yml` (written by `make render`;
  config-init copies the template before the first render) and its webhook URL from a file (`url_file`), so the config
  never holds the URL. The receiver formerly named `default` is now `notify`.
- After upgrading: `make schema`, `make render`.

## 1.0.0 (2026-11-01)

The first versioned release: what a single-VM deployment offers an AI platform, with every device change approved by
a person.

### Features
- **Approved changes** (`WRITE_MODE=approved`, the default with writes on): an agent stages a change; a person with the
  approve right approves exactly that change on the approvals path (its own token audience, a sign-in under 5 minutes
  old, four eyes optional); it is committed once, within its window, with its fingerprint unchanged. Rollback once,
  within an hour. Rights (propose, approve, commit, commit any) are roles in the token. Protected lines are refused at
  staging. Every action is recorded with the person and the calling client. `docs/how-to/approve-changes.md`.
- **Learning that a change waits**: `changes{state}` on the automation API's `/metrics`, and the
  `ChangeWaitingForApproval` alert.
- **`CHANGE_FRESH_SIGNIN_CLAIM`** (`auth_time` or `iat`), for identity providers that record no `auth_time` on the
  password grant.
- `WRITE_MODE=direct` keeps the one-step `push_device_config` for platforms that need it.

### Security
- The automation API is published on loopback by default (`AUTOMATION_BIND_IP`): it has no authentication of its own,
  and its one-step push is refused in approved mode.

### Included from before 1.0.0
Source of truth (Infrahub), observability (Telegraf, Logstash, Prometheus, Loki, Alertmanager, Grafana), the automation
API with assurance rules, and seven MCP servers (inventory, metrics, logs, dashboards, device state and changes,
assurance, pyATS) for Cisco IOS-XE, Huawei VRP and MikroTik RouterOS. Earlier history is in `git log`.
