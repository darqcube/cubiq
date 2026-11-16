"""scripts/ai-platform-connect.py — generated on the platform's host as
agenticops.yml, read by install.py on the stack. Offline: a throwaway local HTTP server plays the
identity provider.

The contract that matters: what the generator writes, install.py accepts —
same reader, same validation — and the issuer survives untouched while only
the key URL is rewritten to something the VM can reach.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ai-platform-connect.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return load(SCRIPT, "ai_platform_connect")


@pytest.fixture(scope="module")
def inst():
    return load(ROOT / "install.py", "installer")


@pytest.fixture
def idp():
    """An OpenID discovery endpoint on loopback, issuer naming localhost."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            body = json.dumps({
                "issuer": f"http://localhost:{port}/realms/demo",
                "jwks_uri": f"http://localhost:{port}/realms/demo/protocol/openid-connect/certs",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/realms/demo"
    server.shutdown()


def test_uses_only_the_standard_library():
    """It runs on the ai-platform host from a bare clone, before any pip."""
    tree = ast.parse(SCRIPT.read_text())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    # install: the repository's own installer (standard library only too), whose reader reads what this writes.
    third_party = imported - set(sys.stdlib_module_names) - {"__future__", "install"}
    assert not third_party, f"non-stdlib imports: {third_party}"


@pytest.mark.parametrize("uri,expected", [
    ("http://localhost:7080/realms/r/certs", "http://192.0.2.1:7080/realms/r/certs"),
    ("http://127.0.0.1:7080/realms/r/certs", "http://192.0.2.1:7080/realms/r/certs"),
    ("http://idp:8080/realms/r/certs", "http://192.0.2.1:8080/realms/r/certs"),
    ("https://idp.example.com/realms/r/certs", "https://idp.example.com/realms/r/certs"),
])
def test_only_unreachable_key_hosts_are_rewritten(gen, uri, expected):
    assert gen.reachable_jwks(uri, "192.0.2.1") == expected


def test_a_given_key_host_always_wins(gen):
    """--jwks-host: a name only this machine resolves (e.g. a VM's .local name) is replaced too."""
    assert gen.reachable_jwks("http://idp.vm.local:7080/r/certs", "192.0.2.9", always=True) == "http://192.0.2.9:7080/r/certs"


def run(idp, tmp_path, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--stack-host", "127.0.0.1", "--idp", idp, "--jwks-host", "192.0.2.1",
         "--out", str(tmp_path), *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_end_to_end_output_is_what_install_accepts(gen, inst, idp, tmp_path):
    out = run(idp, tmp_path, "--audience", "cubiq", "--token-header", "X-Test-Token",
              "--client", "platform-control=execute", "--client", "platform-preprod=read")
    assert out.returncode == 0, out.stderr

    path = tmp_path / "agenticops.yml"
    conn = inst.read_yaml(path)
    assert conn == yaml.safe_load(path.read_text()), "install.py reads it differently from YAML"
    assert not inst.validate_connection(conn), inst.validate_connection(conn)

    platform = conn["platforms"]["agenticops"]
    assert platform["issuer"].startswith("http://localhost:"), "the issuer must stay as tokens carry it"
    assert platform["jwks_url"].startswith("http://192.0.2.1:"), "keys must be fetched from a reachable host"
    assert platform["clients"] == {"platform-control": "execute", "platform-preprod": "read"}
    assert "alert_webhook" not in platform, "written although not asked for"
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    env = inst.ai_platform_env({}, "192.0.2.10", "13000", conn)
    assert env["MCP_AUTH_MODE"] == "oidc"
    assert json.loads(env["MCP_OIDC_PLATFORMS"])[0]["clients"] == {"platform-control": "execute",
                                                                   "platform-preprod": "read"}
    assert env["MCP_BIND_IP"] == "127.0.0.1", "publishing is the stack owner's (site.yml), not the connection's"


def test_the_alert_receiver_is_written_when_given(gen, inst, idp, tmp_path):
    out = run(idp, tmp_path, "--name", "lab", "--alert-webhook-url", "http://192.0.2.1:9100/alertmanager")
    assert out.returncode == 0, out.stderr
    conn = inst.read_yaml(tmp_path / "agenticops.yml")
    assert not inst.validate_connection(conn), inst.validate_connection(conn)
    assert conn["platforms"]["lab"]["alert_webhook"] == "http://192.0.2.1:9100/alertmanager"
    assert inst.connection_env(conn)["PLATFORM_ALERT_WEBHOOKS"] == "lab=http://192.0.2.1:9100/alertmanager"
    assert "--platform-token lab" in out.stdout, "the next steps say how the stack gets the webhook's token"


def test_a_second_platform_is_merged_and_the_first_kept(inst, idp, tmp_path):
    """Two platforms on two identity providers: here the same provider, so the second is told apart by a staging
    realm name only in the file; what matters is the merge."""
    first = tmp_path / "first"
    assert run(idp, first, "--name", "lab", "--client", "platform-control=execute").returncode == 0
    existing = first / "agenticops.yml"
    existing.write_text(existing.read_text() + "overlay:\n  services:\n    infrahub:\n      port: 9001\n"
                        "      protocol: http\n")
    conn = inst.read_yaml(existing)
    conn["platforms"]["lab"]["issuer"] = "http://idp.lab.example/realms/demo"   # a different provider
    lines = ["platforms:", "  lab:"] + [f"    {k}: {json.dumps(v)}" for k, v in conn["platforms"]["lab"].items()
                                        if not isinstance(v, dict)]
    lines += ["    clients:", "      platform-control: execute",
              "overlay:", "  services:", "    infrahub:", "      port: 9001", "      protocol: http"]
    existing.write_text("\n".join(lines) + "\n")

    second = tmp_path / "second"
    out = run(idp, second, "--name", "staging", "--client", "platform-control=read", "--merge", str(existing))
    assert out.returncode == 0, out.stderr
    merged = inst.read_yaml(second / "agenticops.yml")
    assert not inst.validate_connection(merged), inst.validate_connection(merged)
    assert sorted(merged["platforms"]) == ["lab", "staging"]
    assert merged["platforms"]["lab"]["clients"] == {"platform-control": "execute"}
    assert merged["platforms"]["staging"]["clients"] == {"platform-control": "read"}
    assert merged["overlay"]["services"]["infrahub"] == {"port": 9001, "protocol": "http"}


def test_merging_an_old_file_is_refused(idp, tmp_path):
    old = tmp_path / "old.yml"
    old.write_text("access:\n  issuer: http://i\n")
    out = run(idp, tmp_path / "out", "--merge", str(old))
    assert out.returncode == 1 and "before platforms:" in out.stderr


def test_a_bad_client_is_refused(idp, tmp_path):
    out = run(idp, tmp_path, "--client", "platform-control=admin")
    assert out.returncode == 2 and "CLIENT_ID=execute" in out.stderr
    assert not (tmp_path / "agenticops.yml").exists()


def test_snippet_registers_all_seven_servers(gen, tmp_path):
    snippet = yaml.safe_load(gen.mcp_snippet("192.0.2.10", "X-Test-Token"))
    servers = snippet["mcp_servers"]
    assert [s["endpoint"] for s in servers] == [f"http://192.0.2.10:{p}/mcp" for p in range(9001, 9008)]
    assert all(s["transport"] == "http" for s in servers)
    assert all(s["credential_sources"][0]["name"] == "X-Test-Token" for s in servers)


def test_snippet_matches_the_servers_compose_defines(gen):
    compose = yaml.safe_load((ROOT / "compose" / "mcp.yaml").read_text())["services"]
    ports = {name.removeprefix("mcp-"): int(spec["environment"]["PORT"]) for name, spec in compose.items()}
    assert {name: port for name, port, _ in gen.SERVERS} == ports


def test_an_unreachable_idp_fails_clearly(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--stack-host", "127.0.0.1",
         "--idp", "http://127.0.0.1:9/realms/none", "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 1 and "cannot read" in out.stderr
    assert not (tmp_path / "agenticops.yml").exists()


@pytest.mark.parametrize("option", ["--auth-mode=oidc", "--graphs", "--allow-write", "--fresh-signin-claim=iat",
                                    "--write-role=x", "--alert-profile=demo", "--max-response-kb=64", "--bind-ip=x"])
def test_a_1x_option_says_where_it_went(idp, tmp_path, option):
    out = run(idp, tmp_path, option)
    assert out.returncode == 1 and "moved to" in out.stderr, out.stderr
    assert not (tmp_path / "agenticops.yml").exists()
