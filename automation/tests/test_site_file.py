"""Site file tests — the per-deployment input that generates .env.

Offline: no Docker, no stack. The site file is how every customer deployment is
configured, so its reader, validation and derivation all get pinned here.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "site.example.yml"


@pytest.fixture(scope="module")
def inst():
    spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def site(inst):
    """The example, with the placeholders filled in."""
    text = EXAMPLE.read_text()
    text = (text.replace("ssh_password: CHANGEME", "ssh_password: pw")
                .replace("auth: CHANGEME", "auth: authpw")
                .replace("priv: CHANGEME", "priv: privpw"))
    tmp = ROOT / ".site-test.yml"
    tmp.write_text(text)
    try:
        yield inst.read_yaml(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# --- the reader ------------------------------------------------------------

def test_reader_agrees_with_pyyaml(inst):
    """install.py parses the site file with a small reader of its own, because
    it must run before pip has been used and PyYAML is not stdlib. This proves
    the subset it supports is genuinely YAML rather than something that merely
    looks like it."""
    assert inst.read_yaml(EXAMPLE) == yaml.safe_load(EXAMPLE.read_text())


def test_reader_handles_the_types_the_file_uses(inst, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text(
        "# a comment\n"
        "top:\n"
        "  text: hello\n"
        '  quoted: "with spaces"\n'
        "  number: 42\n"
        "  yes_flag: true\n"
        "  no_flag: false\n"
        "  empty: \"\"\n"
        "  items:\n"
        "    - one\n"
        "    - two\n"
    )
    got = inst.read_yaml(f)
    assert got == yaml.safe_load(f.read_text())
    assert got["top"]["number"] == 42 and got["top"]["yes_flag"] is True


def test_reader_rejects_a_malformed_line(inst, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text("site:\n  this line has no colon\n")
    with pytest.raises(inst.SiteError, match="expected 'key: value'"):
        inst.read_yaml(f)


# --- validation ------------------------------------------------------------

def test_the_shipped_example_is_only_missing_its_secrets(inst):
    """Everything except the three CHANGEME credentials must already be valid,
    so the first thing an engineer sees is a short, specific list."""
    problems = inst.validate_site(inst.read_yaml(EXAMPLE))
    creds = [p for p in problems if "CHANGEME" in p or "is required" in p]
    others = [p for p in problems if p not in creds and "collector_ip" not in p]
    assert not others, f"the example has problems beyond its placeholders: {others}"


def test_loopback_collector_ip_is_refused(inst, site):
    site["site"]["collector_ip"] = "127.0.0.1"
    assert any("loopback" in p for p in inst.validate_site(site))


def test_collector_ip_not_on_this_host_is_refused(inst, site):
    """The most common deployment mistake: devices send into a black hole."""
    site["site"]["collector_ip"] = "10.255.255.254"
    problems = inst.validate_site(site)
    if inst.local_addresses():        # skip where interfaces cannot be read
        assert any("not an address on this machine" in p for p in problems)


def test_unknown_top_level_key_is_refused(inst, site):
    """A typo'd key would otherwise be silently ignored and the value never
    applied — the worst kind of configuration bug."""
    site["ai-platform"] = {"enabled": True}
    assert any("unknown top-level key" in p for p in inst.validate_site(site))


def test_missing_credentials_are_named_individually(inst, site):
    site["devices"]["ssh_password"] = "CHANGEME"
    site["devices"]["snmpv3"]["auth"] = ""
    problems = inst.validate_site(site)
    assert any("ssh_password" in p for p in problems)
    assert any("snmpv3.auth" in p for p in problems)


def test_bad_cidr_is_refused(inst, site):
    site["site"]["device_subnets"] = ["10.20.10.0/99"]
    assert any("not a valid CIDR" in p for p in inst.validate_site(site))


def test_beyond_the_tested_ceiling_warns(inst, site):
    site["scale"]["expected_devices"] = 5000
    assert any("tested ceiling" in p for p in inst.validate_site(site))


# --- derivation ------------------------------------------------------------

@pytest.mark.parametrize("count,interval,shard,concurrency", [
    (20, "30s", "150", "8"),
    (120, "60s", "150", "16"),
    (400, "60s", "150", "16"),
    (900, "120s", "200", "24"),
])
def test_scale_band_drives_the_collector_settings(inst, site, count, interval, shard, concurrency):
    """expected_devices exists so nobody has to read docs/scale.md and do
    arithmetic at a customer site."""
    site["scale"]["expected_devices"] = count
    env = inst.site_to_env(site)
    assert env["SNMP_INTERVAL"] == interval
    assert env["SNMP_SHARD_SIZE"] == shard
    assert env["AUTOMATION_CONCURRENCY"] == concurrency


def test_standard_ports_toggle(inst, site):
    site["ports"]["standard"] = True
    assert inst.site_to_env(site)["GRAFANA_PORT"] == "3000"
    site["ports"]["standard"] = False
    assert inst.site_to_env(site)["GRAFANA_PORT"] == "13000"


def test_dedicated_flow_range_follows_the_standard_toggle(inst, site):
    site["ports"]["standard"] = True
    env = inst.site_to_env(site)
    assert (env["FLOW_DEDICATED_FIRST"], env["FLOW_DEDICATED_LAST"]) == ("2056", "2105")
    site["ports"]["standard"] = False
    env = inst.site_to_env(site)
    assert (env["FLOW_DEDICATED_FIRST"], env["FLOW_DEDICATED_LAST"]) == ("12056", "12105")


def test_auto_render_is_off_unless_asked_for(inst, site):
    assert "auto-render" not in inst.site_to_env(site)["COMPOSE_PROFILES"]
    site["source_of_truth"] = {"auto_render": True}
    assert "auto-render" in inst.site_to_env(site)["COMPOSE_PROFILES"]


def test_port_overrides_win(inst, site):
    site["ports"]["overrides"] = {"syslog": 514}
    env = inst.site_to_env(site)
    assert env["SYSLOG_PORT"] == "514"
    assert env["GRAFANA_PORT"] == "13000"      # others still follow `standard`


def test_ai_platform_toggles_the_mcp_profile(inst, site):
    site["ai_platform"]["enabled"] = False
    assert "mcp" not in inst.site_to_env(site)["COMPOSE_PROFILES"]
    site["ai_platform"]["enabled"] = True
    assert "mcp" in inst.site_to_env(site)["COMPOSE_PROFILES"]


def test_write_access_is_off_unless_asked_for(inst, site):
    assert inst.site_to_env(site)["MCP_ALLOW_WRITE"] == "false"
    site["ai_platform"]["allow_write"] = True
    assert inst.site_to_env(site)["MCP_ALLOW_WRITE"] == "true"


@pytest.mark.parametrize("key, value", [
    ("changes", {"write_mode": "approved", "four_eyes": True}),
    ("ai_platform.auth.approve_role", "change-approvers"),
    ("ai_platform.auth.propose_role", "x"),
    ("ai_platform.auth.write_role", "x"),
    ("ai_platform.auth.approvals_audience", "x"),
    ("ai_platform.auth.fresh_signin_claim", "iat"),
])
def test_the_1x_guard_settings_are_refused_as_the_platforms(inst, site, key, value):
    """Approval, rights, windows, fresh sign-in, four eyes, management interfaces: the connected platform's policy
    since 2.0.0. A 1.x site file stops with that said, so nobody believes the stack still enforces them."""
    node = site
    *parents, leaf = key.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    problems = inst.validate_site(site)
    assert any(p.startswith(f"{key} left the stack in 2.0.0") and "the platform" in p for p in problems), problems
    assert not any("unknown" in p and leaf in p for p in problems)


def test_no_change_setting_reaches_the_env(inst, site):
    env = inst.site_to_env(site)
    assert not [k for k in env if k.startswith("CHANGE_") or k in ("WRITE_MODE", "MCP_WRITE_ROLE")], sorted(env)


def test_every_derived_key_exists_in_env_example(inst, site):
    """A derived setting with no matching variable would be silently dropped."""
    example = (ROOT / ".env.example").read_text()
    for key in inst.site_to_env(site):
        assert re.search(rf"^{key}=", example, re.M), (
            f"site_to_env produces {key}, which .env.example does not define"
        )


def test_the_site_file_covers_every_value_a_human_must_supply(inst, site):
    """If a REQUIRED value is not derivable from the site file, an engineer
    using one would still get stopped by a prompt."""
    derived = inst.site_to_env(site)
    for var in inst.REQUIRED:
        assert derived.get(var), f"{var} is required but the site file cannot supply it"


def test_no_secret_is_derived_from_the_site_file(inst, site):
    """The eight generated secrets must stay generated — a site file that could
    set them would invite reusing one across customers."""
    assert not (set(inst.site_to_env(site)) & set(inst.GENERATED))


def test_site_yml_is_gitignored():
    """It holds credentials."""
    ignored = (ROOT / ".gitignore").read_text()
    assert re.search(r"^site\.yml$", ignored, re.M), "site.yml is not gitignored"


# --- coercion -------------------------------------------------------------
# Quoting a boolean in YAML makes it a STRING, and a non-empty string is
# truthy. `allow_write: "false"` therefore ENABLED write access to devices —
# the exact opposite of what was written, with nothing to notice it by.

@pytest.mark.parametrize("literal", ['false', '"false"', "'false'", "no", '"no"', "off", "0"])
def test_falsey_spellings_are_all_false(inst, literal, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text(f"ports:\n  standard: {literal}\nai_platform:\n  allow_write: {literal}\n")
    env = inst.site_to_env(inst.read_yaml(f))
    assert env["MCP_ALLOW_WRITE"] == "false", f"{literal} enabled AI write access"
    assert env["GRAFANA_PORT"] == "13000", f"{literal} selected standard ports"


@pytest.mark.parametrize("literal", ["true", '"true"', "yes", "on", "1"])
def test_truthy_spellings_are_all_true(inst, literal, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text(f"ports:\n  standard: {literal}\nai_platform:\n  allow_write: {literal}\n")
    env = inst.site_to_env(inst.read_yaml(f))
    assert env["MCP_ALLOW_WRITE"] == "true"
    assert env["GRAFANA_PORT"] == "3000"


def test_an_ambiguous_boolean_is_refused_not_guessed(inst, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text("ai_platform:\n  allow_write: maybe\n")
    with pytest.raises(inst.SiteError, match="expected true or false"):
        inst.site_to_env(inst.read_yaml(f))


def test_a_bad_device_count_is_reported_not_thrown(inst, site):
    """validate_site runs before site_to_env, so a bad value must come back as
    a listed problem rather than a traceback from the middle of derivation."""
    site["scale"]["expected_devices"] = "many"
    problems = inst.validate_site(site)
    assert any("whole number" in p for p in problems)


def test_a_bad_boolean_is_listed_with_the_other_problems(inst, site):
    site["ai_platform"]["allow_write"] = "sometimes"
    assert any("expected true or false" in p for p in inst.validate_site(site))


def test_port_overrides_accept_strings_or_ints(inst, site):
    site["ports"]["overrides"] = {"syslog": "514"}
    assert inst.site_to_env(site)["SYSLOG_PORT"] == "514"


# --- an ai-platform on another machine -------------------------------------
# Documentation addresses only (RFC 5737): this repo is cloned onto other
# people's networks.

def test_mcp_stays_on_this_host_unless_published(inst, site):
    env = inst.site_to_env(site)
    assert env["MCP_BIND_IP"] == "127.0.0.1"
    assert env["MCP_ALLOWED_HOSTS"] == ""
    assert env["MCP_AUTH_MODE"] == "token"


def test_publishing_defaults_to_the_collector_address(inst, site):
    """bind_ip empty = the address devices already reach this host on, so the
    common case needs no second copy of an IP."""
    site["site"]["collector_ip"] = "192.0.2.10"
    site["ai_platform"]["publish"] = {"enabled": True, "bind_ip": ""}
    env = inst.site_to_env(site)
    assert env["MCP_BIND_IP"] == "192.0.2.10"
    assert env["MCP_ALLOWED_HOSTS"] == "192.0.2.10:*", "published address not in the Host allow-list"


def test_an_explicit_bind_ip_wins(inst, site):
    site["ai_platform"]["publish"] = {"enabled": True, "bind_ip": "192.0.2.20"}
    env = inst.site_to_env(site)
    assert env["MCP_BIND_IP"] == "192.0.2.20" and env["MCP_ALLOWED_HOSTS"] == "192.0.2.20:*"


def test_grafana_links_use_an_address_a_person_can_open(inst, site):
    site["site"]["collector_ip"] = "192.0.2.10"
    assert inst.site_to_env(site)["GRAFANA_PUBLIC_URL"] == "http://192.0.2.10:13000"


CONNECTION = {
    "access": {
        "issuer": "http://idp.example/realms/demo",
        "jwks_url": "http://192.0.2.1:7080/realms/demo/certs",
        "audiences": "cubiq",
        "clients": {"platform-control": "execute", "platform-preprod": "read"},
    },
    "alerts": {"webhook": "http://192.0.2.1:9100/alerts"},
}


def connection(**changes):
    """A valid agenticops.yml, as read, with the given sections replaced."""
    import copy
    out = copy.deepcopy(CONNECTION)
    out.update(changes)
    return out


# --- standalone: no agenticops.yml ------------------------------------------

def test_standalone_without_a_connection_file(inst, site):
    """The stack is complete on its own: shared token, no identity provider, no platform webhook."""
    assert not inst.validate_ai_platform(site)
    env = inst.site_to_env(site)
    assert env["MCP_AUTH_MODE"] == "token"
    assert env["MCP_OIDC_ISSUER"] == env["MCP_OIDC_JWKS_URL"] == env["MCP_OIDC_CLIENT_SCOPES"] == ""
    assert env["PLATFORM_ALERT_WEBHOOK_URL"] == ""
    assert env["MCP_OIDC_DEFAULT_SCOPE"] == "read"


def test_the_example_connection_file_is_valid_and_agrees_with_pyyaml(inst):
    path = ROOT / "agenticops.example.yml"
    ours = inst.read_yaml(path)
    assert ours == yaml.safe_load(path.read_text()), "the stdlib reader and PyYAML disagree on agenticops.example.yml"
    assert not inst.validate_connection(ours)


def test_the_connection_file_is_gitignored():
    import subprocess
    out = subprocess.run(["git", "check-ignore", "agenticops.yml"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, "agenticops.yml names this deployment's addresses: it must be gitignored"


# --- agenticops.yml -----------------------------------------------------------

def test_a_connection_gives_oidc_and_each_clients_scope(inst, site):
    site["ai_platform"]["auth"].pop("mode", None)
    conn = connection()
    assert not inst.validate_ai_platform(site, conn) and not inst.validate_connection(conn)
    env = inst.site_to_env(site, conn)
    assert env["MCP_AUTH_MODE"] == "oidc", "access: without a mode line means oidc"
    assert env["MCP_OIDC_ISSUER"] == "http://idp.example/realms/demo"
    assert env["MCP_OIDC_JWKS_URL"] == "http://192.0.2.1:7080/realms/demo/certs"
    assert env["MCP_OIDC_CLIENT_SCOPES"] == "platform-control=execute,platform-preprod=read"
    assert env["PLATFORM_ALERT_WEBHOOK_URL"] == "http://192.0.2.1:9100/alerts"


def test_the_platform_webhook_is_separate_from_the_stacks_own(inst, site):
    site.setdefault("alerts", {})["webhook_url"] = "http://192.0.2.50/hook"
    env = inst.site_to_env(site, connection())
    assert env["ALERT_WEBHOOK_URL"] == "http://192.0.2.50/hook"
    assert env["PLATFORM_ALERT_WEBHOOK_URL"] == "http://192.0.2.1:9100/alerts"


def test_access_needs_issuer_and_jwks(inst):
    problems = inst.validate_connection(connection(access={"clients": {"c": "read"}}))
    assert any("access.issuer is required" in p for p in problems)
    assert any("access.jwks_url is required" in p for p in problems)


def test_audiences_accept_a_list_or_a_csv(inst, site):
    for value in (["a", "b"], "a,b", "a, b"):
        conn = connection(access={**CONNECTION["access"], "audiences": value})
        assert inst.site_to_env(site, conn)["MCP_OIDC_AUDIENCES"] == "a,b"


def test_execute_only_for_named_clients(inst):
    """No default_scope of execute: a person's token, or any client, never gets the execute tools by default."""
    problems = inst.validate_connection(connection(access={**CONNECTION["access"], "default_scope": "execute"}))
    assert any("default_scope 'execute'" in p for p in problems)
    problems = inst.validate_connection(connection(access={**CONNECTION["access"], "clients": {"x": "admin"}}))
    assert any("access.clients.x 'admin'" in p for p in problems)


def test_an_unknown_key_in_the_connection_is_refused(inst):
    problems = inst.validate_connection({**CONNECTION, "changes": {"four_eyes": True}})
    assert any("unknown key 'changes'" in p for p in problems)
    problems = inst.validate_connection(connection(alerts={"webhok": "http://x"}))
    assert any("unknown key 'webhok'" in p for p in problems)


def test_a_platform_webhook_must_be_a_url(inst):
    assert any("alerts.webhook" in p for p in inst.validate_connection(connection(alerts={"webhook": "not a url"})))


def test_overlay_services_are_checked(inst):
    good = connection(overlay={"services": {"infrahub": {"port": 9001, "protocol": "http"}}})
    assert not inst.validate_connection(good)
    bad = connection(overlay={"services": {"Infra_Hub": {"port": 0, "protocol": "udp"}}})
    problems = inst.validate_connection(bad)
    assert any("a DNS label" in p for p in problems)
    assert any("port must be 1 to 65535" in p for p in problems)
    assert any("protocol" in p for p in problems)


def test_oidc_without_a_connection_is_refused(inst, site):
    site["ai_platform"]["auth"]["mode"] = "oidc"
    assert any("needs agenticops.yml" in p for p in inst.validate_ai_platform(site, {}))


def test_token_mode_would_ignore_a_connection(inst, site):
    site["ai_platform"]["auth"]["mode"] = "token"
    assert any("would ignore it" in p for p in inst.validate_ai_platform(site, connection()))


# --- 1.x settings: refused, saying where they went ---------------------------

@pytest.mark.parametrize("key, value, where", [
    ("auth.issuer", "http://idp.example/realms/demo", "access.issuer"),
    ("auth.jwks_url", "http://192.0.2.1/certs", "access.jwks_url"),
    ("auth.audiences", "a", "access.audiences"),
    ("auth.agent_claim", "azp", "access.agent_claim"),
    ("alert_webhook_url", "http://192.0.2.1/a", "alerts.webhook"),
    ("alert_profile", "demo", "alerts.profile"),
])
def test_a_moved_key_names_where_it_went(inst, site, key, value, where):
    node = site["ai_platform"]
    *parents, leaf = key.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    problems = inst.validate_ai_platform(site)
    assert any(f"ai_platform.{key} moved to" in p and where in p for p in problems), problems
    assert not any("unknown key" in p for p in problems), "a moved key must be explained, not called unknown"


def test_the_1x_connection_file_stops_the_install(inst, tmp_path, monkeypatch, capsys):
    old = tmp_path / "sites" / "ai-platform.yml"
    old.parent.mkdir()
    old.write_text("ai_platform:\n  publish:\n    enabled: true\n")
    monkeypatch.setattr(inst, "ROOT", tmp_path)
    monkeypatch.setattr(inst, "OLD_OVERLAY", old)
    monkeypatch.setattr(inst, "configure_from_site", lambda *a, **k: pytest.fail("configured over a 1.x file"))
    inst.configure(type("Args", (), {"from_site": None, "agenticops": None})())
    out = capsys.readouterr().out
    assert "no longer read" in out and "agenticops.yml" in out


def test_the_1x_option_is_refused_with_a_pointer(tmp_path):
    import subprocess, sys
    out = subprocess.run([sys.executable, str(ROOT / "install.py"), "--ai-platform", "x.yml", "--check"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode != 0 and "--agenticops" in out.stderr


# --- the rest of ai_platform: still the stack's own --------------------------

def test_an_unknown_auth_mode_or_key_is_refused(inst, site):
    site["ai_platform"]["auth"]["mode"] = "open"
    site["ai_platform"]["publish"]["bind"] = "x"          # typo for bind_ip
    problems = inst.validate_ai_platform(site)
    assert any("auth.mode 'open'" in p for p in problems)
    assert any("unknown key 'bind'" in p for p in problems)


def test_a_loopback_bind_ip_is_refused(inst, site):
    site["ai_platform"]["publish"] = {"enabled": True, "bind_ip": "127.0.0.1"}
    assert any("loopback" in p for p in inst.validate_ai_platform(site))


def test_max_response_is_bounded_and_in_bytes(inst, site):
    site["ai_platform"]["max_response_kb"] = 64
    assert inst.site_to_env(site)["MCP_MAX_RESPONSE_BYTES"] == str(64 * 1024)
    site["ai_platform"]["max_response_kb"] = 1
    assert any("max_response_kb" in p for p in inst.validate_ai_platform(site))


def test_set_env_values_replaces_and_appends(inst):
    text = "A=1\nMCP_AUTH_MODE=token   # comment\n"
    out = inst.set_env_values(text, {"MCP_AUTH_MODE": "oidc", "MCP_OIDC_ISSUER": "http://i"})
    assert "MCP_AUTH_MODE=oidc\n" in out and "A=1\n" in out
    assert out.rstrip().endswith("MCP_OIDC_ISSUER=http://i"), "a key missing from an older .env was not added"


def test_a_connection_applies_to_an_existing_env(inst, tmp_path, monkeypatch):
    """An install done without a site file still takes agenticops.yml — and only its keys change."""
    env = tmp_path / ".env"
    env.write_text("SYSLOG_COLLECTOR_IP=192.0.2.10\nMCP_BIND_IP=192.0.2.10\nMCP_AUTH_MODE=token\n"
                   "DEVICE_PASSWORD=keep\nMCP_ALLOW_WRITE=false\nALERT_WEBHOOK_URL=http://192.0.2.50/keep\n")
    conn = tmp_path / "agenticops.yml"
    conn.write_text("access:\n  issuer: http://idp.example/realms/demo\n"
                    "  jwks_url: http://192.0.2.1:7080/realms/demo/certs\n"
                    "  clients:\n    platform-control: execute\n"
                    "alerts:\n  webhook: http://192.0.2.1:9100/alerts\n")
    monkeypatch.setattr(inst, "ENV", env)
    inst.apply_connection_to_env(conn)
    values = inst.read_env(env)
    assert values["MCP_AUTH_MODE"] == "oidc"
    assert values["MCP_OIDC_JWKS_URL"] == "http://192.0.2.1:7080/realms/demo/certs"
    assert values["MCP_OIDC_CLIENT_SCOPES"] == "platform-control=execute"
    assert values["PLATFORM_ALERT_WEBHOOK_URL"] == "http://192.0.2.1:9100/alerts"
    assert values["ALERT_WEBHOOK_URL"] == "http://192.0.2.50/keep", "the stack's own receiver is not the connection's"
    assert values["DEVICE_PASSWORD"] == "keep" and values["MCP_ALLOW_WRITE"] == "false"
    assert values["MCP_BIND_IP"] == "192.0.2.10", "publishing is site.yml's, not the connection's"


def test_a_bad_connection_changes_nothing(inst, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MCP_AUTH_MODE=token\n")
    conn = tmp_path / "agenticops.yml"
    conn.write_text("access:\n  issuer: not-a-url\n")
    monkeypatch.setattr(inst, "ENV", env)
    inst.apply_connection_to_env(conn)
    assert env.read_text() == "MCP_AUTH_MODE=token\n"


def test_graphs_are_off_unless_asked_for(inst, site):
    env = inst.site_to_env(site)
    assert env["MCP_GRAPHS_ENABLED"] == "false" and env["GRAFANA_RENDERER_URL"] == ""
    assert "graphs" not in env["COMPOSE_PROFILES"]


def test_graphs_start_the_renderer_and_link_on_the_published_address(inst, site):
    site["site"]["collector_ip"] = "192.0.2.10"
    site["ai_platform"]["publish"] = {"enabled": True, "bind_ip": ""}
    site["ai_platform"]["graphs"] = True
    env = inst.site_to_env(site)
    assert env["MCP_GRAPHS_ENABLED"] == "true"
    assert "graphs" in env["COMPOSE_PROFILES"].split(",")
    assert env["GRAFANA_RENDERER_URL"] == "http://grafana-renderer:8081/render"
    assert env["MCP_IMAGE_BASE_URL"] == "http://192.0.2.10:9004"


def test_graphs_without_publish_are_refused(inst, site):
    """A person's browser opens the images: unpublished, they cannot load."""
    site["ai_platform"]["graphs"] = True
    assert any("ai_platform.graphs needs ai_platform.publish" in p for p in inst.validate_ai_platform(site))


def test_alert_profile_defaults_to_standard(inst, site):
    site.get("alerts", {}).pop("profile", None)
    assert inst.site_to_env(site)["ALERT_PROFILE"] == "standard"


def test_alert_profile_comes_from_the_site(inst, site):
    site.setdefault("alerts", {})["profile"] = "demo"
    assert inst.site_to_env(site)["ALERT_PROFILE"] == "demo"


def test_an_unknown_alert_profile_is_refused(inst, site):
    site.setdefault("alerts", {})["profile"] = "fast"
    assert any("alert profile 'fast'" in p for p in inst.validate_ai_platform(site))
