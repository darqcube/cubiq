# Mark which alerts should start an AI investigation

Since 2.0.0 the stack sends **every alert to every receiver**
([change-alerts.md](change-alerts.md#sending-alerts-somewhere)): which alert
matters to whom, and which one starts an AI investigation, is the connected
platform's decision, not the stack's.

The stack still keeps the place to record that intent per device, as data in
Infrahub, so a platform can read it (through the Infrahub MCP server or
GraphQL) and decide with it. Three attributes on each **Device**, off unless
set:

| Attribute | Value | Empty means |
|---|---|---|
| `auto_investigate` | on / off (default **off**) | – |
| `auto_investigate_interfaces` | comma-separated `ifName` patterns, e.g. `Tu.*, Et0/1` | every interface |
| `auto_investigate_protocols` | comma-separated: `bgp`, `ospf`, `isis`, `eigrp`, `rip` | every protocol the stack watches |

A pattern is meant as a regular expression for the **whole** name, as the device
reports it in IF-MIB `ifName`. IOS-XE reports short names (`Gi1`, `Tu2`,
`Et0/1`). The alerts these attributes are about: `DeviceUnreachable`,
`InterfaceDown`, `BGPPeerDown`, `OSPFNeighborDown` (BGP and OSPF are the
protocols the stack watches today).

Set them in the Infrahub UI, or in the device's YAML:

```yaml
# source-of-truth/devices/<yours>.yml
- name: router1
  role: core-wan
  auto_investigate: true
  auto_investigate_interfaces: "Tu.*, Et0/1"
  auto_investigate_protocols: bgp
```

```bash
make seed      # when the YAML changed
```

## Moving from 1.x

1.x turned these attributes into Alertmanager routes: an `investigate` receiver
for the alerts they named, `notify` for the rest. In 2.0.0 there is no
`investigate` receiver: every alert goes to `notify` (the stack's own,
`ALERT_WEBHOOK_URL`) and to `platform` (each connected platform's,
`agenticops.yml` `platforms.<name>.alert_webhook`). The attributes and their values stay as they
were; a platform that acted on `"receiver": "investigate"` reads them instead.

## Maintenance

A silence in the Alertmanager UI stops both receivers. Set a device's `status`
to `maintenance` to stop polling it.
