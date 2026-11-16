#!/usr/bin/env python3
"""Generate the files that connect AgenticOps (or another platform) to this stack.

Run this ON THE PLATFORM'S HOST, from a clone of this repository. Everything
the stack needs to trust the platform lives on that side — its identity
provider's issuer and signing keys, and the address the stack can reach it on
— so it is read there rather than typed by hand on the stack's machine.

    python3 scripts/ai-platform-connect.py \\
        --stack-host 192.0.2.10 --name production \\
        --idp http://localhost:7080/realms/<realm> \\
        --client platform-control=execute

A second platform (a staging copy, say) is added to the same file with
--merge agenticops.yml --name staging: the platforms already in it, and its
overlay, are kept; one of the same name is replaced.

Writes two files (default: the current directory):

    agenticops.yml             copy to the stack's directory, then run
                               install.py there (absent: the stack is standalone)
    mcp-servers.snippet.yaml   the seven MCP servers, to register in the
                               platform's own MCP server configuration

Standard library only, like install.py: it runs before anything is installed.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "templates" / "ai-platform-mcp-servers.yaml.tmpl"

# name, port, what the ai-platform gets from it — the same table as
# docs/how-to/connect-an-ai-platform.md.
SERVERS = [
    ("infrahub", 9001, "Intended state: devices, sites, applications and their dependencies"),
    ("prometheus", 9002, "Live metrics, alerts, interface state and traffic flows"),
    ("loki", 9003, "Device syslog: search, recent errors, pattern counts"),
    ("grafana", 9004, "Dashboard links to hand a person"),
    ("netmiko", 9005, "Running config and parsed operational state from the device"),
    ("assurance", 9006, "Assurance rule checks, snapshots and parsed config"),
    ("pyats", 9007, "Genie structured models: BGP neighbors, interfaces, LLDP, platform"),
]
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def discover(idp: str) -> dict:
    """The provider's OpenID configuration: issuer and jwks_uri."""
    url = idp.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            config = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        fail(f"cannot read {url}: {exc}\n"
             f"  Is the ai-platform's identity provider running, and is --idp the realm URL?")
    for key in ("issuer", "jwks_uri"):
        if not config.get(key):
            fail(f"{url} has no '{key}'")
    return config


def address_towards(host: str) -> str:
    """This machine's address on the route to `host`.

    UDP connect() only selects a route; nothing is sent. That address is how
    the stack will reach this machine's identity provider for its keys.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError as exc:
        fail(f"no route to {host}: {exc}")


def reachable_jwks(jwks_uri: str, local_ip: str, always: bool = False) -> str:
    """The JWKS URL with a loopback or container-only hostname replaced (any hostname, with `always`: --jwks-host
    given, e.g. a name only this machine resolves).

    The issuer often names `localhost` (it is what a browser on this machine
    uses), which from the stack VM means the VM itself. The ISSUER must stay
    exactly as the tokens carry it; only the URL the keys are fetched from
    changes.
    """
    parts = urllib.parse.urlsplit(jwks_uri)
    host = parts.hostname or ""
    if always or host in LOOPBACK_NAMES or "." not in host:
        netloc = local_ip + (f":{parts.port}" if parts.port else "")
        return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    return jwks_uri


def token_claims(path: Path) -> dict:
    """Claims from a sample token — unverified, only to suggest settings."""
    try:
        payload = path.read_text().strip().split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (OSError, IndexError, ValueError) as exc:
        fail(f"{path} is not a JWT: {exc}")


# Options of the 1.x generator, and where each went in 2.0.0: named, so an old
# invocation says what to change instead of failing on an unknown option.
REMOVED_OPTIONS = {
    "--auth-mode": "the stack's site.yml (ai_platform.auth.mode; oidc by default once agenticops.yml gives access)",
    "--bind-ip": "the stack's site.yml (ai_platform.publish)",
    "--graphs": "the stack's site.yml (ai_platform.graphs)",
    "--max-response-kb": "the stack's site.yml (ai_platform.max_response_kb)",
    "--alert-profile": "the stack's site.yml (alerts.profile)",
    "--allow-write": "the stack's site.yml (ai_platform.allow_write), or make writes-on on the stack host",
    "--write-role": "the platform: since 2.0.0 the stack checks scopes (--client), not roles",
    "--fresh-signin-claim": "the platform: since 2.0.0 the stack checks scopes (--client), not approvals",
}


def parse_client(text: str) -> tuple[str, str]:
    """--client ID=SCOPE."""
    cid, sep, scope = text.partition("=")
    if not sep or not cid.strip() or scope.strip() not in ("read", "execute"):
        raise argparse.ArgumentTypeError(f"{text!r}: expected CLIENT_ID=read or CLIENT_ID=execute")
    return cid.strip(), scope.strip()


PLATFORM_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?")


def platform_name(text: str) -> str:
    if not PLATFORM_NAME.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r}: lower case, digits and '-' (up to 32), e.g. production")
    return text


def read_existing(path: Path) -> dict:
    """An agenticops.yml to merge into, read with install.py's own reader (standard library only)."""
    sys.path.insert(0, str(ROOT))
    import install  # noqa: E402 - the stack's installer: its reader is the one that will read the result
    try:
        found = install.read_yaml(path)
    except install.SiteError as exc:
        fail(f"{path}: {exc}")
    for old in ("access", "alerts"):
        if old in found:
            fail(f"{path} is from before platforms: (it has '{old}:'): generate it again without --merge")
    return found


def q(value) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def platform_lines(name: str, spec: dict) -> list[str]:
    lines = [f"  {name}:", f"    issuer: {q(spec['issuer'])}", f"    jwks_url: {q(spec['jwks_url'])}"]
    audiences = spec.get("audiences") or ""
    audiences = ",".join(audiences) if isinstance(audiences, list) else str(audiences)
    lines.append(f"    audiences: {q(audiences)}")
    lines.append(f"    default_scope: {spec.get('default_scope') or 'read'}")
    if spec.get("clients"):
        lines.append("    clients:")
        lines += [f"      {cid}: {scope}" for cid, scope in sorted(spec["clients"].items())]
    lines.append(f"    agent_claim: {spec.get('agent_claim') or 'azp'}")
    if spec.get("alert_webhook"):
        lines.append(f"    alert_webhook: {q(spec['alert_webhook'])}")
    return lines


def connection_file(args, issuer: str, jwks_url: str, audiences: list[str], existing: dict | None = None) -> str:
    """agenticops.yml, in the block-style YAML install.py reads: this platform, plus (with --merge) the others and
    the overlay of the existing file."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    existing = existing or {}
    listed = dict(existing.get("platforms") or {})
    listed[args.name] = {"issuer": issuer, "jwks_url": jwks_url, "audiences": audiences,
                         "default_scope": args.default_scope, "clients": dict(args.client),
                         "agent_claim": args.agent_claim, "alert_webhook": args.alert_webhook_url}
    lines = [
        f"# Generated {stamp} by scripts/ai-platform-connect.py on the platform's host.",
        "# Copy to the stack's directory as agenticops.yml (gitignored) and run",
        "# python3 install.py --step 2 there. No secrets in this file: each alert",
        "# webhook's token is set there with python3 install.py --platform-token NAME.",
        "platforms:",
    ]
    for name in sorted(listed):
        lines += platform_lines(name, listed[name])
    services = (existing.get("overlay") or {}).get("services") or {}
    if services:
        lines += ["overlay:", "  services:"]
        for svc, spec in sorted(services.items()):
            lines += [f"    {svc}:", f"      port: {spec.get('port')}", f"      protocol: {spec.get('protocol') or 'http'}"]
    return "\n".join(lines) + "\n"


def mcp_snippet(stack_host: str, token_header: str) -> str:
    template = TEMPLATE.read_text()
    entries = []
    block = template.split("# --- per server ---\n", 1)[1]
    head = template.split("# --- per server ---\n", 1)[0]
    for name, port, description in SERVERS:
        entries.append(block.format(name=name, port=port, description=description,
                                    stack_host=stack_host, token_header=token_header))
    return head + "".join(entries)


def probe(host: str) -> list[str]:
    """Which MCP ports answer from here — before publishing, none will."""
    open_ports = []
    for name, port, _ in SERVERS:
        try:
            with socket.create_connection((host, port), timeout=2):
                open_ports.append(f"{name}:{port}")
        except OSError:
            pass
    return open_ports


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack-host", required=True,
                    help="the stack VM's address, as reachable from this machine")
    ap.add_argument("--name", type=platform_name, default="agenticops",
                    help="this platform's name in agenticops.yml and the stack's audit log (default: agenticops)")
    ap.add_argument("--merge", type=Path, metavar="FILE",
                    help="an existing agenticops.yml: keep its other platforms and its overlay, replace --name's")
    ap.add_argument("--idp", required=True,
                    help="the identity provider realm URL tokens are issued by, "
                         "e.g. http://localhost:7080/realms/<realm>")
    ap.add_argument("--audience", action="append", default=[],
                    help="an accepted 'aud' value; repeat for several (default: from "
                         "--token-file, else not checked)")
    ap.add_argument("--token-file", type=Path,
                    help="a sample access token, to suggest audiences and show its roles")
    ap.add_argument("--jwks-host",
                    help="address the stack uses to reach this machine (default: detected)")
    ap.add_argument("--client", action="append", default=[], type=parse_client, metavar="ID=SCOPE",
                    help="a machine client by OIDC client id and its scope, read or execute; repeat for several. "
                         "Give execute to the platform's control service only")
    ap.add_argument("--default-scope", default="read", choices=("read", "none"),
                    help="what any other valid token may do (default: read)")
    ap.add_argument("--agent-claim", default="azp", help="the claim naming the calling agent (default: azp)")
    ap.add_argument("--alert-webhook-url", default="",
                    help="where the stack also sends every alert (the platform's alert endpoint, without any secret: "
                         "its token is set on the stack with install.py --platform-token NAME); omit for none")
    ap.add_argument("--token-header", default="",
                    help="request header your ai-platform forwards the caller's token in, "
                         "for the MCP server snippet (see its credential-source docs)")
    ap.add_argument("--stack-user", default="<user>", help="SSH user on the stack VM, for the copy command")
    ap.add_argument("--stack-dir", default="~/cubiq",
                    help="the repository's path on the stack's machine")
    ap.add_argument("--out", type=Path, default=Path("."), help="where to write the two files")
    old = [opt for opt in REMOVED_OPTIONS if any(a == opt or a.startswith(opt + "=") for a in sys.argv[1:])]
    if old:
        fail("options from before 2.0.0:\n" + "\n".join(f"  {opt}: moved to {REMOVED_OPTIONS[opt]}" for opt in old))
    args = ap.parse_args()

    config = discover(args.idp)
    issuer = config["issuer"]
    local_ip = args.jwks_host or address_towards(args.stack_host)
    jwks_url = reachable_jwks(config["jwks_uri"], local_ip, always=bool(args.jwks_host))

    audiences = list(args.audience)
    if args.token_file:
        claims = token_claims(args.token_file)
        if claims.get("iss") != issuer:
            print(f"warning: the sample token's iss {claims.get('iss')!r} differs from the "
                  f"provider's issuer {issuer!r} — the stack would refuse such tokens")
        aud = claims.get("aud") or []
        aud = [aud] if isinstance(aud, str) else aud
        client = claims.get(args.agent_claim) or claims.get("azp")
        scope = dict(args.client).get(client, args.default_scope)
        print(f"sample token: aud={aud} {args.agent_claim}={client!r} -> scope {scope}")
        if not audiences:
            audiences = aud

    args.out.mkdir(parents=True, exist_ok=True)
    overlay = args.out / "agenticops.yml"
    snippet = args.out / "mcp-servers.snippet.yaml"
    existing = read_existing(args.merge) if args.merge else None
    overlay.write_text(connection_file(args, issuer, jwks_url, audiences, existing))
    overlay.chmod(0o600)
    snippet.write_text(mcp_snippet(args.stack_host, args.token_header or "<token-header>"))

    print(f"platform   {args.name}" + (f" (merged into {args.merge})" if args.merge else ""))
    print(f"issuer     {issuer}")
    print(f"jwks_url   {jwks_url}   (as the stack will fetch it)")
    print(f"audiences  {','.join(audiences) or '(not checked)'}")
    print(f"clients    {', '.join(f'{c}={s}' for c, s in sorted(dict(args.client).items())) or 'none'}"
          f" · any other token: {args.default_scope}")
    print(f"wrote      {overlay}")
    print(f"wrote      {snippet}")
    already = probe(args.stack_host)
    print(f"MCP ports open on {args.stack_host} now: {', '.join(already) or 'none (expected before step 2)'}")
    print()
    print("Next:")
    print(f"  1. scp {overlay} {args.stack_user}@{args.stack_host}:{args.stack_dir}/agenticops.yml")
    print("  2. on the stack: without an overlay network, publish the MCP servers in site.yml")
    print("     (ai_platform.publish.enabled: true), then:")
    print(f"     cd {args.stack_dir} && python3 install.py --step 2 && make mcp-apply")
    print(f"  3. register the servers in {snippet} with the platform")
    if not args.token_header:
        print("     — set --token-header to fill in the header the ai-platform forwards tokens in")
    print(f"  4. from the stack VM, check it can fetch the keys:  curl -s {jwks_url}")
    if args.alert_webhook_url:
        print(f"  5. on the stack, the token the alert webhook expects (from stdin, never in a file there):")
        print(f"     python3 install.py --platform-token {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
