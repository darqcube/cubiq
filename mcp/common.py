"""Shared MCP scaffolding: transport, auth, bounds, backend calls.

THE TWO RULES EVERY SERVER FOLLOWS

1. No passthrough tools. No tool takes a raw PromQL, LogQL, GraphQL or CLI
   string. Every query is composed server-side from validated arguments. One
   passthrough tool makes every other boundary in the stack decorative, and it
   is very hard to remove once something depends on it.

2. Every argument is bounded before use. Device names against a regex, time
   windows and result limits capped here rather than trusted from the caller.

WHO MAY CALL — MCP_AUTH_MODE

  token   two shared secrets (default): MCP_AUTH_TOKEN gives the read scope,
          MCP_EXECUTE_TOKEN the execute scope. No identity, no provider.
  oidc    the caller's own JWT from the connected platform's identity provider
          (agenticops.yml access:), checked against its JWKS. Every call is
          attributable; the token's client (azp) has the scope MCP_OIDC_CLIENT_SCOPES
          names, any other the default (MCP_OIDC_DEFAULT_SCOPE: read or none).
  both    either. For cut-over, and for tooling on the host that has the tokens.

WHAT A SCOPE ALLOWS

  read     every read tool
  execute  also the tools that change a device (stage, commit, rollback, withdraw)

That is all the stack decides. Whether a person approved a change, and who may
ask for one, is the connected platform's policy: authentication only, here.
"""
from __future__ import annotations

import functools
import hmac
import inspect
import json
import logging
import os
import re
from typing import Any

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

CUBIQ_VERSION = os.environ.get("CUBIQ_VERSION", "").strip() or "unknown"
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
EXECUTE_TOKEN = os.environ.get("MCP_EXECUTE_TOKEN", "")
AUTH_MODE = (os.environ.get("MCP_AUTH_MODE") or "token").strip().lower()
OIDC_ISSUER = os.environ.get("MCP_OIDC_ISSUER", "").strip()
OIDC_JWKS_URL = os.environ.get("MCP_OIDC_JWKS_URL", "").strip()
OIDC_AUDIENCES = [a.strip() for a in os.environ.get("MCP_OIDC_AUDIENCES", "").split(",") if a.strip()]
# The claim naming the calling agent, when the platform gives each agent its own
# client (token exchange). Recorded on every change and in the audit line.
AGENT_CLAIM = os.environ.get("MCP_AGENT_CLAIM", "").strip() or "azp"

SCOPES = ("read", "execute")
DEFAULT_SCOPE = (os.environ.get("MCP_OIDC_DEFAULT_SCOPE") or "read").strip().lower()


def parse_client_scopes(raw: str) -> dict[str, str]:
    """MCP_OIDC_CLIENT_SCOPES: "client-id=scope,…" (install.py writes it from agenticops.yml)."""
    found: dict[str, str] = {}
    for pair in (p.strip() for p in raw.split(",") if p.strip()):
        client, sep, scope = pair.partition("=")
        if not sep or not client.strip() or scope.strip().lower() not in SCOPES:
            raise SystemExit(f"MCP_OIDC_CLIENT_SCOPES: {pair!r} — expected client-id=read or client-id=execute")
        found[client.strip()] = scope.strip().lower()
    return found


CLIENT_SCOPES = parse_client_scopes(os.environ.get("MCP_OIDC_CLIENT_SCOPES", ""))
if DEFAULT_SCOPE not in ("read", "none"):
    raise SystemExit(f"MCP_OIDC_DEFAULT_SCOPE must be read or none (got {DEFAULT_SCOPE!r}): "
                     "execute is given to named clients only")

# 512 KiB suits a large model. A small local model's whole context can be
# smaller than that, so a deployment can lower it (site.yml max_response_kb).
MAX_RESPONSE_BYTES = int(os.environ.get("MCP_MAX_RESPONSE_BYTES") or 512 * 1024)

AUTH_MODES = ("token", "oidc", "both")

# Longer than common proxy pool idle timeouts (60 s and below), see serve().
KEEP_ALIVE_SECONDS = 75

log = logging.getLogger("mcp.audit")

# Device and label values that reach a backend query. Deliberately strict:
# these arrive from an AI platform, not from a person.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DURATION = re.compile(r"^[0-9]{1,4}[smhd]$")


class BoundsError(ValueError):
    """An argument failed validation before it reached a backend."""


def identifier(value: str, what: str = "name") -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise BoundsError(f"invalid {what}: {value!r}")
    return value


def duration(value: str, default: str = "1h", max_hours: int = 24) -> str:
    """A time window, capped. An unbounded range is how a tool call turns into
    a multi-gigabyte backend query."""
    value = value or default
    if not _DURATION.fullmatch(value):
        raise BoundsError(f"invalid duration: {value!r} (use e.g. 15m, 6h, 2d)")
    n, unit = int(value[:-1]), value[-1]
    hours = {"s": n / 3600, "m": n / 60, "h": n, "d": n * 24}[unit]
    if hours > max_hours:
        raise BoundsError(f"duration {value} exceeds the {max_hours}h limit")
    return value


def limit(value: int | None, default: int = 100, maximum: int = 1000) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


# Interface names carry slashes (Et0/0, GigabitEthernet1/0/1) that identifier()
# rightly refuses. Still a closed alphabet: no spaces, quotes or braces, so a
# name can sit inside a PromQL label matcher or a URL parameter safely.
_INTERFACE = re.compile(r"^[A-Za-z][A-Za-z0-9_./:-]{0,63}$")


def interface_name(value: str) -> str:
    if not value or not _INTERFACE.fullmatch(value):
        raise BoundsError(f"invalid interface: {value!r}")
    return value


def literal(value: str | None, what: str = "term", maximum: int = 256) -> str | None:
    """A free-text search term, length-capped. It is escaped where it is used;
    this only stops a caller sending a megabyte of it."""
    if value is not None and len(value) > maximum:
        raise BoundsError(f"{what} is {len(value)} characters, over the {maximum} limit")
    return value


class Backend:
    """HTTP client for one backing service, with a hard response cap."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers=headers or {})

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, json: dict | None = None) -> Any:
        return self._request("POST", path, json=json)

    def get_bytes(self, path: str, params: dict | None = None, *, max_bytes: int,
                  content_type: str) -> bytes:
        """Raw bytes of one expected type — a rendered image, never JSON.

        Its own cap: MAX_RESPONSE_BYTES bounds what reaches a MODEL, and an
        image never does (the tool returns a link to it).
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._client.get(url, params=params)
        except httpx.RequestError as exc:
            raise RuntimeError(f"{self.base_url} unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {response.status_code}: {response.text[:300]}")
        got = response.headers.get("content-type", "")
        if not got.startswith(content_type):
            raise RuntimeError(f"GET {path} returned {got or 'no content type'}, not {content_type}")
        if len(response.content) > max_bytes:
            raise RuntimeError(f"GET {path} returned {len(response.content)} bytes, over {max_bytes}")
        return response.content

    def _request(self, method: str, path: str, **kw) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._client.request(method, url, **kw)
        except httpx.RequestError as exc:
            raise RuntimeError(f"{self.base_url} unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")

        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError(
                f"{path} returned {len(body)} bytes, over the "
                f"{MAX_RESPONSE_BYTES} limit — narrow the query"
            )
        try:
            return response.json()
        except ValueError:
            return response.text


def flatten(node: Any) -> Any:
    """Strip Infrahub's {"value": x} envelope, recursively.

    Infrahub wraps every attribute, so an 11-device query becomes thousands of
    characters of {"name": {"value": "router1"}} noise. A model reading that
    miscounts and misquotes; flattened, it reads what is actually there.
    """
    if isinstance(node, dict):
        if set(node) == {"value"}:
            return node["value"]
        if "value" in node and len(node) <= 2 and "__typename" in node:
            return node["value"]
        return {k: flatten(v) for k, v in node.items() if k != "__typename"}
    if isinstance(node, list):
        return [flatten(v) for v in node]
    return node


# --- who is calling --------------------------------------------------------

class AuthError(Exception):
    """The request carries no acceptable credential."""


class Verifier:
    """Decides who a bearer credential belongs to, and its scope, per MCP_AUTH_MODE.

    Returns a caller dict: {"user", "sub", "via", "agent", "scope"}.
    """

    def __init__(self, mode: str = AUTH_MODE, token: str = AUTH_TOKEN, issuer: str = OIDC_ISSUER,
                 jwks_url: str = OIDC_JWKS_URL, audiences: list[str] | None = None,
                 execute_token: str = EXECUTE_TOKEN, client_scopes: dict[str, str] | None = None,
                 default_scope: str = DEFAULT_SCOPE):
        if mode not in AUTH_MODES:
            raise SystemExit(f"MCP_AUTH_MODE must be one of {', '.join(AUTH_MODES)} (got {mode!r})")
        # Fail at startup, not at the first request: an empty token used to
        # mean "no auth at all", silently.
        if mode in ("token", "both") and not token:
            raise SystemExit(f"MCP_AUTH_MODE={mode} needs MCP_AUTH_TOKEN")
        if execute_token and hmac.compare_digest(execute_token, token):
            raise SystemExit("MCP_EXECUTE_TOKEN must differ from MCP_AUTH_TOKEN: the read token would execute")
        if mode in ("oidc", "both") and not (issuer and jwks_url):
            raise SystemExit(f"MCP_AUTH_MODE={mode} needs MCP_OIDC_ISSUER and MCP_OIDC_JWKS_URL")
        self.mode, self.token, self.issuer = mode, token, issuer
        self.execute_token = execute_token
        self.client_scopes = client_scopes if client_scopes is not None else CLIENT_SCOPES
        self.default_scope = default_scope
        self.audiences = audiences if audiences is not None else OIDC_AUDIENCES
        self._jwks = None
        if mode in ("oidc", "both"):
            import jwt  # pyjwt[crypto] — only needed when OIDC is on

            self._jwt = jwt
            # Keys are cached, and refetched when a token names an unknown kid,
            # so an identity-provider key rotation needs no restart.
            self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=10)

    def verify(self, header: str) -> dict:
        if not header.lower().startswith("bearer "):
            raise AuthError("missing bearer credential")
        credential = header[7:].strip()

        if self.mode in ("token", "both"):
            if hmac.compare_digest(credential, self.token):
                return {"user": "token", "sub": "token", "via": "token", "agent": None, "scope": "read"}
            if self.execute_token and hmac.compare_digest(credential, self.execute_token):
                return {"user": "execute-token", "sub": "execute-token", "via": "token", "agent": None,
                        "scope": "execute"}
        if self.mode == "token":
            raise AuthError("bad token")
        return self._verify_jwt(credential)

    def _verify_jwt(self, credential: str) -> dict:
        jwt = self._jwt
        try:
            key = self._jwks.get_signing_key_from_jwt(credential).key
            claims = jwt.decode(
                credential, key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                issuer=self.issuer,
                audience=self.audiences or None,
                options={"require": ["exp", "iss", "sub"], "verify_aud": bool(self.audiences)},
                leeway=30,
            )
        except jwt.PyJWKClientError as exc:
            raise AuthError(f"cannot fetch signing keys: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid token: {exc}") from exc

        # The client the token was issued to decides the scope: never a role, a group or a person's name.
        client = claims.get("azp") or claims.get("client_id")
        scope = self.client_scopes.get(client, self.default_scope) if isinstance(client, str) else self.default_scope
        if scope not in SCOPES:
            raise AuthError(f"client {client!r} has no scope here")
        return {
            "user": claims.get("preferred_username") or claims.get("email") or claims["sub"],
            "sub": claims["sub"],
            "via": "oidc",
            "agent": claims.get(AGENT_CLAIM) if isinstance(claims.get(AGENT_CLAIM), str) else None,
            "scope": scope,
        }


class _Auth:
    """Bearer auth plus one audit line per MCP request.

    Plain ASGI rather than BaseHTTPMiddleware: it must not buffer the
    streaming responses streamable-HTTP sends. /healthz is exempt so
    orchestration can probe it.

    The caller is stored on the request scope, where a tool reads it through
    its MCP Context (caller()). A ContextVar would not reach the tool: in a
    stateful session the tool runs in the session's task, not the request's.
    """

    def __init__(self, app, verifier: Verifier, server: str, public_prefixes: tuple[str, ...] = ()):
        self.app, self.verifier, self.server = app, verifier, server
        # Paths that authorise themselves (an image link is its own
        # capability: a browser <img> cannot send a bearer token).
        self.public_prefixes = public_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in ("/healthz", "/version"):
            return await self.app(scope, receive, send)
        if self.public_prefixes and scope["path"].startswith(self.public_prefixes):
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        try:
            caller = self.verifier.verify(headers.get("authorization", ""))
        except AuthError as exc:
            log.warning("audit denied server=%s reason=%s", self.server, exc)
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await response(scope, receive, send)

        scope.setdefault("state", {})["mcp_caller"] = caller

        body = bytearray()

        async def recording_receive():
            message = await receive()
            if message.get("type") == "http.request" and len(body) < 65536:
                body.extend(message.get("body", b""))
            return message

        await self.app(scope, recording_receive, send)
        # Prefixed so `grep audit` finds it whatever log format is active —
        # the MCP SDK configures a bare %(message)s format.
        log.info("audit user=%s sub=%s via=%s agent=%s scope=%s server=%s %s", caller["user"], caller["sub"],
                 caller["via"], caller.get("agent") or "-", caller["scope"], self.server, _rpc_summary(bytes(body)))


def _rpc_summary(body: bytes) -> str:
    """`rpc=tools/call tool=get_device_state` — what was asked, never the arguments."""
    try:
        message = json.loads(body or b"{}")
    except ValueError:
        return "rpc=?"
    messages = message if isinstance(message, list) else [message]
    parts = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        part = f"rpc={m.get('method', 'response')}"
        tool = (m.get("params") or {}).get("name") if m.get("method") == "tools/call" else None
        parts.append(part + (f" tool={tool}" if tool else ""))
    return " ".join(parts) or "rpc=-"


def caller(ctx: Context) -> dict:
    """The authenticated caller of the current tool call."""
    request = getattr(ctx.request_context, "request", None)
    found = getattr(getattr(request, "state", None), "mcp_caller", None)
    if not found:
        raise PermissionError("no authenticated caller on this request")
    return found


def require_scope(ctx: Context, scope: str = "execute") -> dict:
    """Refuse the call unless the caller's credential has `scope` (execute covers read)."""
    who = caller(ctx)
    if who["scope"] == scope or who["scope"] == "execute":
        return who
    raise PermissionError(f"{who['user']} has the {who['scope']} scope; this tool needs {scope} "
                          "(the execute token, or a client agenticops.yml gives execute)")


# --- the server ------------------------------------------------------------

def _in_thread(fn):
    """Run a blocking tool in a worker thread.

    FastMCP calls a sync tool directly on the event loop, so one slow device
    call (a config fetch can take minutes) would stall every other request to
    this server — including health checks and other agents' calls.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return wrapper


class _Server(FastMCP):
    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            return register(fn if inspect.iscoroutinefunction(fn) else _in_thread(fn))
        return decorator


def allowed_hosts(name: str) -> list[str]:
    """Host headers this server answers to.

    The MCP SDK refuses any other Host with 421, as DNS-rebinding protection.
    Always: loopback and this server's own compose name. Plus MCP_ALLOWED_HOSTS
    — the address an ai-platform on another machine uses (install.py derives
    it from ai_platform.publish.bind_ip). Patterns are `host:port` or `host:*`.
    """
    hosts = ["localhost:*", "127.0.0.1:*", "[::1]:*", f"mcp-{name}:*"]
    for item in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        item = item.strip()
        if item and item not in hosts:
            hosts.append(item)
    return hosts


def build(name: str) -> FastMCP:
    # host= is set because FastMCP's default of 127.0.0.1 silently turns on a
    # loopback-only Host allow-list, and every call through the compose
    # network then fails with 421 while /healthz stays green. The explicit
    # allow-list below replaces it. (uvicorn's bind address is in serve().)
    return _Server(
        name,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts(name),
            allowed_origins=[],
        ),
    )


def serve(mcp: FastMCP, port: int, public_routes: list[Route] | tuple = ()) -> None:
    """Run over streamable-HTTP with auth and a health endpoint.

    `public_routes` are served WITHOUT the bearer check and must authorise
    themselves; each route's path up to its first parameter becomes an exempt
    prefix (e.g. /g/{name} -> /g/).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")
    verifier = Verifier()
    app = mcp.streamable_http_app()
    app.router.routes.append(
        Route("/healthz", lambda r: PlainTextResponse("ok"), methods=["GET"])
    )
    # Which Cubiq this is (install.py writes CUBIQ_VERSION from the checkout's tag), so a connected platform can check
    # it needs no newer one. Public like /healthz: a version, never a secret.
    app.router.routes.append(
        Route("/version", lambda r: JSONResponse({"name": "cubiq", "version": CUBIQ_VERSION}), methods=["GET"])
    )
    app.router.routes.extend(public_routes)
    prefixes = tuple(r.path.split("{", 1)[0] for r in public_routes)
    import uvicorn

    print(f"auth mode: {verifier.mode}", flush=True)
    # A gateway in front (an ai-platform's MCP proxy) reuses pooled keep-alive
    # connections. uvicorn closes idle ones after 5 s by default, so a reused
    # connection can be closed mid-request: "connection closed before message
    # completed", intermittently. Outlive the proxy's pool instead.
    uvicorn.run(_Auth(app, verifier, mcp.name, prefixes), host="0.0.0.0", port=port, log_level="info",
                timeout_keep_alive=KEEP_ALIVE_SECONDS)
