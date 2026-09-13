"""OAuth: provider behaviour, and the full HTTP flow Claude performs -- dynamic
registration, authorize, password login, PKCE token exchange, bearer-authenticated
MCP calls, refresh rotation and revocation -- against the real Starlette app."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from mcp.server.auth.provider import AccessToken, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from mealie_sous_chef import server
from mealie_sous_chef.auth import MAX_FAILED_LOGINS, MealieOAuthProvider, _redirect_uri_allowed
from mealie_sous_chef.config import Settings
from mealie_sous_chef.mealie import MealieClient

PASSWORD = "correct-horse-battery-staple"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
BASE = "https://sous-chef.test"


# ---------------------------------------------------------------- provider


@pytest.mark.parametrize(
    "uri, allowed",
    [
        (CALLBACK, True),
        ("https://claude.ai/api/mcp/auth_callback/", False),
        ("https://evil.example/api/mcp/auth_callback", False),
        ("http://claude.ai/api/mcp/auth_callback", False),
        ("http://localhost:53682/callback", True),  # loopback: any port
        ("http://127.0.0.1:1234/callback", True),
        ("http://localhost:53682/other", False),
        ("http://127.0.0.2:1234/callback", False),
    ],
)
def test_redirect_uri_allowlist(uri, allowed):
    assert _redirect_uri_allowed(uri, Settings().mcp_allowed_redirect_uris) is allowed


def _client(client_id="c1", redirect=CALLBACK):
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=[redirect], token_endpoint_auth_method="none")


async def test_register_rejects_foreign_redirects(tmp_path):
    provider = MealieOAuthProvider(Settings(mcp_data_dir=str(tmp_path)))
    with pytest.raises(RegistrationError):
        await provider.register_client(_client(redirect="https://evil.example/cb"))
    await provider.register_client(_client())
    assert await provider.get_client("c1") is not None


async def test_state_persists_and_expired_tokens_are_pruned(tmp_path):
    settings = Settings(mcp_data_dir=str(tmp_path))
    provider = MealieOAuthProvider(settings)
    await provider.register_client(_client())
    token = provider._issue("c1", ["mealie"], None, "owner")
    provider.access_tokens["stale"] = AccessToken(token="stale", client_id="c1", scopes=["mealie"], expires_at=int(time.time()) - 5)
    provider._save()

    reloaded = MealieOAuthProvider(settings)
    assert await reloaded.get_client("c1") is not None
    assert await reloaded.load_access_token(token.access_token) is not None
    assert "stale" not in reloaded.access_tokens
    assert await reloaded.load_refresh_token(_client("someone-else"), token.refresh_token) is None


async def test_corrupt_state_file_starts_clean(tmp_path):
    (tmp_path / "auth_state.json").write_text("{nope")
    provider = MealieOAuthProvider(Settings(mcp_data_dir=str(tmp_path)))
    assert provider.clients == {}


# ---------------------------------------------------------------- HTTP flow


@pytest.fixture
def http(fake, monkeypatch):
    server.oauth.clients.clear()
    server.oauth.access_tokens.clear()
    server.oauth.refresh_tokens.clear()
    server.oauth.auth_codes.clear()
    server.oauth.pending.clear()
    server.oauth._failed_logins.clear()
    monkeypatch.setattr(
        server, "MealieClient", lambda url, token: MealieClient(url, token, transport=fake.transport())
    )
    app = server.mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")
    with TestClient(app, base_url=BASE) as client:
        yield client


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _register(http) -> str:
    r = http.post(
        "/register",
        json={
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Claude",
            "scope": "mealie",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize(http, client_id: str, challenge: str) -> str:
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "scope": "mealie",
            "resource": f"{BASE}/mcp",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    login = urlsplit(r.headers["location"])
    assert login.path == "/login"
    return parse_qs(login.query)["state"][0]


def _mcp(http, token: str | None, method: str, params: dict | None = None, rid: int = 1):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def _login_and_get_tokens(http) -> tuple[str, dict]:
    client_id = _register(http)
    verifier, challenge = _pkce()
    state = _authorize(http, client_id, challenge)
    page = http.get("/login", params={"state": state})
    assert page.status_code == 200 and "Claude" in page.text

    wrong = http.post("/login", data={"state": state, "password": "nope"})
    assert wrong.status_code == 200 and "Wrong password." in wrong.text

    ok = http.post("/login", data={"state": state, "password": PASSWORD}, follow_redirects=False)
    assert ok.status_code == 302
    redirect = urlsplit(ok.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == CALLBACK
    query = parse_qs(redirect.query)
    assert query["state"] == ["client-state"]

    token_request = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": f"{BASE}/mcp",
    }
    tokens = http.post("/token", data=token_request)
    assert tokens.status_code == 200, tokens.text
    replay = http.post("/token", data=token_request)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    return client_id, tokens.json()


def test_metadata_is_public_and_canonical(http):
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] in (BASE, f"{BASE}/")
    assert meta["token_endpoint"] == f"{BASE}/token" and "S256" in meta["code_challenge_methods_supported"]
    assert http.get("/").text.startswith("Mealie Sous Chef MCP server.")


def test_mcp_requires_a_valid_bearer_token(http):
    r = _mcp(http, None, "tools/list")
    assert r.status_code == 401 and "Bearer" in r.headers.get("www-authenticate", "")
    assert _mcp(http, "made-up-token", "tools/list").status_code == 401


def test_full_oauth_flow_then_tool_calls_refresh_and_revoke(http, fake):
    client_id, tokens = _login_and_get_tokens(http)
    access = tokens["access_token"]

    init = _mcp(http, access, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert init.status_code == 200, init.text
    listed = _mcp(http, access, "tools/list", rid=2)
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"review_recipe_ingredients", "edit_recipe_ingredients", "manage_taxonomy", "search_recipes"} <= names

    fake.add_food("rice")
    called = _mcp(http, access, "tools/call", {"name": "manage_taxonomy", "arguments": {"resource": "foods"}}, rid=3)
    body = called.json()["result"]
    assert body.get("isError") is not True and body["structuredContent"]["total"] == 1

    failing = _mcp(http, access, "tools/call", {"name": "update_recipe", "arguments": {"slug": "x"}}, rid=4)
    result = failing.json()["result"]
    assert result["isError"] is True and "nothing to update" in result["content"][0]["text"]

    # refresh rotation: the new pair works, the old refresh token is dead
    refreshed = http.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert refreshed.status_code == 200, refreshed.text
    again = http.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert again.status_code == 400
    new_access = refreshed.json()["access_token"]
    assert _mcp(http, new_access, "tools/list").status_code == 200

    # the SDK's RevocationRequest requires the client_secret field even for public clients
    revoked = http.post("/revoke", data={"token": new_access, "client_id": client_id, "client_secret": ""})
    assert revoked.status_code == 200
    assert _mcp(http, new_access, "tools/list").status_code == 401


def test_login_lockout_after_repeated_failures(http):
    client_id = _register(http)
    _, challenge = _pkce()
    state = _authorize(http, client_id, challenge)
    for _ in range(MAX_FAILED_LOGINS):
        assert "Wrong password." in http.post("/login", data={"state": state, "password": "nope"}).text
    locked = http.post("/login", data={"state": state, "password": PASSWORD}, follow_redirects=False)
    assert locked.status_code == 429


def test_expired_or_unknown_login_state(http):
    assert http.get("/login", params={"state": "bogus"}).status_code == 400
    assert http.post("/login", data={"state": "bogus", "password": PASSWORD}).status_code == 400
