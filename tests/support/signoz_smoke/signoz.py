"""Credentials, dashboards and ingestion checks on the ephemeral SigNoz, through its HTTP API.

A brand-new SigNoz has no user: the first registration creates the organization and its root
admin, with no invitation or e-mail. From there, everything is automatic and lives only as
long as the stack:

1. ``POST /api/v1/register``: the admin, with a random password made for this run;
2. ``POST /api/v2/sessions/email_password``: a session token (the UI login does the same);
3. ``POST /api/v1/service_accounts`` + ``/api/v1/service_account_roles`` (``signoz-editor``)
   + ``/api/v1/service_accounts/{id}/keys``: an API key with the Editor role, exactly what
   ``scripts/signoz_dashboards.py --apply`` uses in production (``SIGNOZ-API-KEY``).

No production credential is read; the ``.env`` is not used.
"""

import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr
from scripts import signoz_dashboards

from tests.support.signoz_smoke.seed import MS, Summary

ADMIN_EMAIL = "smoke-admin@example.test"
ORGANIZATION = "trade-agent-smoke"
EDITOR_ROLE = "signoz-editor"


@dataclass(frozen=True, slots=True)
class Credentials:
    email: str
    password: str
    org_id: str
    api_key: str


def _data(response: httpx.Response) -> Any:
    if response.is_error:
        request = response.request
        raise RuntimeError(f"{request.method} {request.url.path}: {response.text}")
    return response.json()["data"]


def bootstrap(url: str) -> Credentials:
    password = f"Smoke-{secrets.token_urlsafe(18)}-1!"
    with httpx.Client(base_url=url, timeout=30) as http:
        admin = {
            "name": "Smoke Tests",
            "orgName": ORGANIZATION,
            "email": ADMIN_EMAIL,
            "password": password,
        }
        org_id = _data(http.post("/api/v1/register", json=admin))["orgId"]
        login = {"email": ADMIN_EMAIL, "password": password, "orgId": org_id}
        token = _data(http.post("/api/v2/sessions/email_password", json=login))["accessToken"]
        http.headers["Authorization"] = f"Bearer {token}"
        account = _data(http.post("/api/v1/service_accounts", json={"name": "smoke-tests"}))
        roles = {role["name"]: role["id"] for role in _data(http.get("/api/v1/roles"))}
        binding = {"serviceAccountId": account["id"], "roleId": roles[EDITOR_ROLE]}
        _data(http.post("/api/v1/service_account_roles", json=binding))
        key = _data(
            http.post(
                f"/api/v1/service_accounts/{account['id']}/keys",
                json={"name": "smoke-tests", "expiresAt": 0},
            )
        )["key"]
    return Credentials(ADMIN_EMAIL, password, org_id, key)


def api(url: str, credentials: Credentials) -> httpx.Client:
    """The same client ``scripts/signoz_dashboards.py`` builds from the ``.env``."""
    settings = signoz_dashboards.SigNozSettings(
        api_key=SecretStr(credentials.api_key), url=url, _env_file=None
    )
    return signoz_dashboards.client(settings)


def provision(http: httpx.Client) -> dict[str, str]:
    """Applies every dashboard of the generator, as ``--apply`` does; ``name`` → ``id``."""
    signoz_dashboards.apply(http, signoz_dashboards.build())
    return signoz_dashboards.existing(http)


def count(http: httpx.Client, signal: str, where: str, summary: Summary) -> int:
    """``count()`` of logs or traces over the seeded window."""
    body = {
        "schemaVersion": "v1",
        "start": summary.start // MS,
        "end": summary.end // MS + 60_000,
        "requestType": "scalar",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": signoz_dashboards.events(signal, "count()", where=where),
                }
            ]
        },
    }
    data = _data(http.post("/api/v5/query_range", json=body))
    results = data["data"]["results"]
    rows = results[0].get("data") or [] if results else []
    return int(rows[0][-1]) if rows else 0


def wait_ingested(http: httpx.Client, summary: Summary, *, timeout: float = 180) -> None:
    """Waits until SigNoz answers with everything that was sent (the collector batches)."""
    expected = {
        ("traces", "service.name LIKE 'trade-agent.%'"): summary.spans,
        ("logs", "service.namespace = 'trade-agent'"): summary.logs,
    }
    deadline = time.monotonic() + timeout
    found: dict[tuple[str, str], int] = {}
    while time.monotonic() < deadline:
        found = {key: count(http, *key, summary) for key in expected}
        if found == expected:
            return
        time.sleep(2)
    raise RuntimeError(f"ingestion incomplete: expected {expected}, found {found}")
