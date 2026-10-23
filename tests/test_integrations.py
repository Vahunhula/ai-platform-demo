"""Deterministic Demo 3 Phase 1 provider, security, and API tests."""

import base64
import dataclasses
import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_platform.api.app import create_app
from ai_platform.auth import Role
from ai_platform.integrations.http import HttpResponse, ProviderHttpError
from ai_platform.integrations.models import (
    IntegrationConfigurationError,
    IntegrationProvider,
)
from ai_platform.integrations.service import IntegrationService
from tests.test_messaging import _context, provision

REFRESH_SECRET = "SUPER_SECRET_REFRESH_TOKEN"
ACCESS_SECRET = "SUPER_SECRET_ACCESS_TOKEN"
ROTATED_REFRESH_SECRET = "ROTATED_SUPER_SECRET_REFRESH_TOKEN"
GITHUB_TOKEN = "SUPER_SECRET_GITHUB_INSTALLATION_TOKEN"


class FakeProviderHttp:
    def __init__(self) -> None:
        self.github_missing = False
        self.repository_count = 2
        self.refresh_count = 0
        self.invalid_refresh = False
        self.requests: list[tuple[str, str, dict | None]] = []
        self._lock = threading.Lock()

    def request(self, method, url, *, headers=None, body=None):  # noqa: ANN001, ANN201
        self.requests.append((method, url, body))
        if "/app/installations/" in url and not url.endswith("/access_tokens"):
            if self.github_missing:
                raise ProviderHttpError(404)
            return HttpResponse(
                200,
                {
                    "id": 123456,
                    "account": {"id": 42, "login": "Butopea", "type": "Organization"},
                    "repository_selection": "selected",
                },
            )
        if url.endswith("/access_tokens"):
            return HttpResponse(
                201, {"token": GITHUB_TOKEN, "expires_at": "2026-10-23T12:00:00Z"}
            )
        if "/installation/repositories" in url:
            return HttpResponse(
                200,
                {
                    "total_count": self.repository_count,
                    "repositories": [
                        {"id": index, "name": f"repo-{index}"}
                        for index in range(self.repository_count)
                    ],
                },
            )
        if url == "https://auth.atlassian.com/oauth/token":
            if body["grant_type"] == "refresh_token":
                if self.invalid_refresh:
                    raise ProviderHttpError(403)
                with self._lock:
                    self.refresh_count += 1
                return HttpResponse(
                    200,
                    {
                        "access_token": f"ROTATED_ACCESS_{self.refresh_count}",
                        "refresh_token": ROTATED_REFRESH_SECRET,
                        "expires_in": 3600,
                        "scope": "read:me read:jira-user offline_access",
                    },
                )
            return HttpResponse(
                200,
                {
                    "access_token": ACCESS_SECRET,
                    "refresh_token": REFRESH_SECRET,
                    "expires_in": 3600,
                    "scope": "read:me read:jira-user offline_access",
                },
            )
        if url.endswith("/oauth/token/accessible-resources"):
            return HttpResponse(
                200,
                [
                    {
                        "id": "cloud-one",
                        "name": "Company Jira",
                        "url": "https://company.atlassian.net",
                        "scopes": ["read:jira-user"],
                    },
                    {
                        "id": "cloud-two",
                        "name": "Sandbox Jira",
                        "url": "https://sandbox.atlassian.net",
                        "scopes": ["read:jira-user"],
                    },
                ],
            )
        if url == "https://api.atlassian.com/me":
            return HttpResponse(200, {"account_id": "acct-7", "name": "Alex Admin"})
        if "/rest/api/3/myself" in url:
            return HttpResponse(200, {"accountId": "acct-7", "displayName": "Alex Admin"})
        raise AssertionError(f"Unexpected fake request: {method} {url}")


def _configured_context(tmp_path: Path):  # noqa: ANN202
    context = _context(tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    settings = dataclasses.replace(
        context.settings,
        integration_encryption_key=key,
        github_app_id="999",
        github_app_slug="ai-platform-test",
        github_app_private_key=pem,
        github_app_webhook_secret="webhook-test-secret",
        jira_oauth_client_id="jira-client",
        jira_oauth_client_secret="jira-client-secret",
        jira_oauth_redirect_uri="http://test/api/integrations/jira/callback",
    )
    fake = FakeProviderHttp()
    context.settings = settings
    context.integrations = IntegrationService(settings, context.storage, http=fake)
    return context, fake


async def _login(app, username: str, role: Role) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ANN001
    token = provision(app, username, role)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)
    response = await client.post("/api/auth/login", json={"username": username, "token": token})
    assert response.status_code == 200
    return client


def _state(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


def _db_text(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return "\n".join(connection.iterdump())


@pytest.mark.anyio
async def test_unconfigured_startup_and_admin_only_management(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    developer = await _login(app, "dev", Role.DEVELOPER)
    assert (await developer.get("/api/integrations")).status_code == 403
    assert (await developer.post("/api/integrations/github/connect")).status_code == 403
    await developer.aclose()

    admin = await _login(app, "admin", Role.ADMIN)
    response = await admin.get("/api/integrations")
    assert response.status_code == 200
    assert [item["status"] for item in response.json()] == ["NOT_CONFIGURED", "NOT_CONFIGURED"]
    assert (await admin.get("/api/health")).json() == {"status": "ok"}
    await admin.aclose()


@pytest.mark.anyio
async def test_github_install_health_webhooks_idempotency_and_disconnect(tmp_path: Path) -> None:
    context, fake = _configured_context(tmp_path)
    app = create_app(context)
    admin = await _login(app, "admin", Role.ADMIN)

    started = await admin.post("/api/integrations/github/connect")
    assert started.status_code == 200
    state = _state(started.json()["authorization_url"])
    assert state not in _db_text(context.settings.db_path)

    # Simulate the cross-site setup return without the Strict session cookie.
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        follow_redirects=False,
    )
    callback = await anonymous.get(
        "/api/integrations/github/callback",
        params={"installation_id": 123456, "state": state, "format": "json"},
    )
    await anonymous.aclose()
    assert callback.status_code == 200
    body = callback.json()
    assert body["account"]["name"] == "Butopea"
    assert body["metadata"]["repository_count"] == 2
    assert GITHUB_TOKEN not in json.dumps(body)
    token_requests = [request for request in fake.requests if request[1].endswith("access_tokens")]
    assert token_requests[-1][2] == {"permissions": {"metadata": "read"}}
    reused = await admin.get(
        "/api/integrations/github/callback",
        params={"installation_id": 123456, "state": state, "format": "json"},
    )
    assert reused.status_code == 400

    health = await admin.post("/api/integrations/github/check")
    assert health.json()["healthy"] is True
    assert health.json()["repository_count"] == 2

    fake.repository_count = 3
    payload = json.dumps(
        {"action": "added", "installation": {"id": 123456}, "repositories_added": []}
    ).encode()
    signature = "sha256=" + hmac.new(b"webhook-test-secret", payload, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "installation_repositories",
        "Content-Type": "application/json",
    }
    assert (await admin.post("/api/webhooks/github", content=payload, headers=headers)).json() == {
        "accepted": True
    }
    assert (await admin.post("/api/webhooks/github", content=payload, headers=headers)).json() == {
        "accepted": False
    }
    assert (
        await admin.post(
            "/api/webhooks/github",
            content=payload,
            headers={**headers, "X-Hub-Signature-256": "sha256=bad"},
        )
    ).status_code == 401
    assert (await admin.get("/api/integrations/github")).json()["metadata"]["repository_count"] == 3

    fake.github_missing = True
    missing = await admin.post("/api/integrations/github/check")
    assert missing.json()["status"] == "REAUTH_REQUIRED"
    await admin.delete("/api/integrations/github")
    assert (await admin.get("/api/integrations/github")).json()["status"] == "DISCONNECTED"
    await admin.aclose()


@pytest.mark.anyio
async def test_jira_state_site_selection_encryption_rotation_and_disconnect(tmp_path: Path) -> None:
    context, fake = _configured_context(tmp_path)
    app = create_app(context)
    admin = await _login(app, "admin", Role.ADMIN)

    expired_start = await admin.post("/api/integrations/jira/connect")
    expired_state = _state(expired_start.json()["authorization_url"])
    with context.storage.transaction(immediate=True) as database:
        database.execute(
            "UPDATE integration_oauth_states SET expires_at=? WHERE state_hash=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                context.integrations.store.state_hash(expired_state),
            ),
        )
    expired_callback = await admin.get(
        "/api/integrations/jira/callback",
        params={"code": "x", "state": expired_state, "format": "json"},
    )
    assert expired_callback.status_code == 400

    started = await admin.post("/api/integrations/jira/connect")
    state = _state(started.json()["authorization_url"])
    assert "read%3Ame" in started.json()["authorization_url"]
    assert "read%3Ajira-user" in started.json()["authorization_url"]
    assert "offline_access" in started.json()["authorization_url"]
    mismatched = await admin.get(
        "/api/integrations/jira/callback",
        params={"code": "x", "state": "wrong", "format": "json"},
    )
    assert mismatched.status_code == 400

    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        follow_redirects=False,
    )
    callback = await anonymous.get(
        "/api/integrations/jira/callback",
        params={"code": "authorization-code", "state": state, "format": "json"},
    )
    await anonymous.aclose()
    assert callback.status_code == 200
    selection = callback.json()
    assert selection["selection_required"] is True
    assert len(selection["sites"]) == 2
    assert REFRESH_SECRET not in callback.text and ACCESS_SECRET not in callback.text

    selected = await admin.post(
        "/api/integrations/jira/site",
        json={"selection_id": selection["selection_id"], "cloud_id": "cloud-one"},
    )
    assert selected.status_code == 200
    assert selected.json()["site"]["url"] == "https://company.atlassian.net"
    assert selected.json()["account"]["name"] == "Alex Admin"
    dump = _db_text(context.settings.db_path)
    assert REFRESH_SECRET not in dump and ACCESS_SECRET not in dump
    assert "jira-client-secret" not in dump and "authorization-code" not in dump

    connection = context.integrations.store.get_connection(IntegrationProvider.JIRA)
    assert connection is not None
    envelope, revision = context.integrations.store.credentials(connection.id) or ("", 0)
    credentials = context.integrations.cipher.decrypt(envelope, context="JIRA")
    credentials["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    expired = context.integrations.cipher.encrypt(credentials, context="JIRA")
    assert context.integrations.store.rotate_credentials(connection.id, revision, expired)

    health = await admin.post("/api/integrations/jira/check")
    assert health.json()["healthy"] is True
    assert fake.refresh_count == 1
    assert REFRESH_SECRET not in _db_text(context.settings.db_path)
    current = context.integrations.store.credentials(connection.id)
    assert current is not None
    rotated = context.integrations.cipher.decrypt(current[0], context="JIRA")
    assert rotated["refresh_token"] == ROTATED_REFRESH_SECRET

    # The single-host refresh mutex plus DB compare-and-swap prevents two workers
    # from overwriting a newly rotated token pair.
    rotated["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    current_revision = current[1]
    assert context.integrations.store.rotate_credentials(
        connection.id,
        current_revision,
        context.integrations.cipher.encrypt(rotated, context="JIRA"),
    )
    second_worker = IntegrationService(context.settings, context.storage, http=fake)
    threads = [
        threading.Thread(target=context.integrations._jira_credentials, args=(connection,)),
        threading.Thread(target=second_worker._jira_credentials, args=(connection,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fake.refresh_count == 2

    latest = context.integrations.store.credentials(connection.id)
    assert latest is not None
    latest_credentials = context.integrations.cipher.decrypt(latest[0], context="JIRA")
    latest_credentials["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert context.integrations.store.rotate_credentials(
        connection.id,
        latest[1],
        context.integrations.cipher.encrypt(latest_credentials, context="JIRA"),
    )
    fake.invalid_refresh = True
    revoked = await admin.post("/api/integrations/jira/check")
    assert revoked.json()["status"] == "REAUTH_REQUIRED"

    await admin.delete("/api/integrations/jira")
    assert context.integrations.store.credentials(connection.id) is None
    assert (await admin.post("/api/integrations/jira/check")).status_code == 400
    await admin.aclose()


def test_missing_and_wrong_encryption_keys_fail_closed(tmp_path: Path) -> None:
    context, _fake = _configured_context(tmp_path)
    actor_app = create_app(context)
    # Persist a recognizable encrypted credential directly through the domain store.
    envelope = context.integrations.cipher.encrypt(
        {"refresh_token": REFRESH_SECRET}, context="JIRA"
    )
    context.integrations.store.save_connection(
        IntegrationProvider.JIRA,
        created_by="admin",
        display_name="Jira",
        external_account_id="acct",
        external_account_name="Admin",
        external_site_id="cloud",
        external_site_url="https://company.atlassian.net",
        encrypted_credentials=envelope,
    )
    assert actor_app is not None

    missing = dataclasses.replace(context.settings, integration_encryption_key=None)
    with pytest.raises(IntegrationConfigurationError, match="key is unavailable"):
        IntegrationService(missing, context.storage, http=FakeProviderHttp())

    wrong_key = base64.urlsafe_b64encode(b"x" * 32).decode()
    with pytest.raises(IntegrationConfigurationError, match="cannot be decrypted"):
        IntegrationService(
            dataclasses.replace(context.settings, integration_encryption_key=wrong_key),
            context.storage,
            http=FakeProviderHttp(),
        )


def test_audit_metadata_redacts_recognizable_secrets(tmp_path: Path) -> None:
    context, _fake = _configured_context(tmp_path)
    context.integrations.store.audit(
        "INTEGRATION_HEALTH_CHECKED",
        IntegrationProvider.JIRA,
        "admin",
        {
            "access_token": ACCESS_SECRET,
            "refresh_token": REFRESH_SECRET,
            "private_key": "SUPER_SECRET_GITHUB_KEY",
            "safe": "CONNECTED",
        },
    )
    serialized = json.dumps(context.integrations.store.audit_events())
    assert "SUPER_SECRET" not in serialized
    assert "CONNECTED" in serialized
