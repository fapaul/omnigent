"""User-installed agents: ``POST`` / ``DELETE /v1/agents`` and their visibility.

A multi-user server must never let one user overwrite, see, remove, or bind
another user's installed agent, nor an operator template (``created_by``
NULL). These run against a strict header-auth provider (no single-user
``"local"`` fallback), the posture of a deployed server.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes._session_create_validation import require_template_visible
from omnigent.server.routes.builtin_agents import create_builtin_agents_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from tests.server.helpers import build_agent_bundle

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}


@pytest.fixture()
def agent_store(db_uri: str) -> SqlAlchemyAgentStore:
    return SqlAlchemyAgentStore(db_uri)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(str(tmp_path / "artifacts"))


def _app(agent_store, artifact_store, tmp_path: Path, auth_provider) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(
        create_builtin_agents_router(
            agent_store,
            AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
            artifact_store=artifact_store,
            auth_provider=auth_provider,
        ),
        prefix="/v1",
    )
    return app


@pytest_asyncio.fixture()
async def client(agent_store, artifact_store, tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    """Client for a multi-user server: identity from ``X-Forwarded-Email``."""
    provider = UnifiedAuthProvider(source="header", local_single_user=False)
    app = _app(agent_store, artifact_store, tmp_path, provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _install(
    client: httpx.AsyncClient, headers: dict[str, str], name: str, description: str = "v1"
) -> httpx.Response:
    bundle = build_agent_bundle(name, description=description)
    return await client.post(
        "/v1/agents",
        headers=headers,
        files={"bundle": ("bundle.tar.gz", bundle, "application/gzip")},
    )


async def _names(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, list[dict]]:
    resp = await client.get("/v1/agents?limit=100", headers=headers)
    assert resp.status_code == 200, resp.text
    by_name: dict[str, list[dict]] = {}
    for row in resp.json()["data"]:
        by_name.setdefault(row["name"], []).append(row)
    return by_name


async def test_install_is_listed_only_for_its_owner(client, agent_store) -> None:
    resp = await _install(client, ALICE, "orion")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["installed"] is True
    assert body["builtin"] is False
    assert agent_store.get(body["id"]).created_by == "alice@example.com"

    assert [r["id"] for r in (await _names(client, ALICE))["orion"]] == [body["id"]]
    assert "orion" not in await _names(client, BOB)


async def test_reinstall_replaces_in_place(client) -> None:
    first = (await _install(client, ALICE, "orion", "v1")).json()
    second = (await _install(client, ALICE, "orion", "v2")).json()
    assert second["id"] == first["id"], "reinstall must keep the agent id stable"
    assert second["version"] == first["version"] + 1
    assert len((await _names(client, ALICE))["orion"]) == 1


async def test_same_name_for_another_user_is_a_separate_row(client, agent_store) -> None:
    alice = (await _install(client, ALICE, "orion", "alice")).json()
    bob = (await _install(client, BOB, "orion", "bob")).json()
    assert bob["id"] != alice["id"]
    assert agent_store.get(alice["id"]).version == 1, "Bob's install touched Alice's agent"


async def test_install_never_overwrites_an_operator_template(
    client, agent_store, artifact_store
) -> None:
    """An unowned (NULL) template can't be matched, so it can't be hijacked."""
    operator_id = generate_agent_id()
    bundle = build_agent_bundle("orion", description="operator")
    location = f"{operator_id}/{hashlib.sha256(bundle).hexdigest()}"
    artifact_store.put(location, bundle)
    agent_store.create(operator_id, "orion", location)

    mine = (await _install(client, ALICE, "orion", "alice")).json()
    assert mine["id"] != operator_id
    operator = agent_store.get(operator_id)
    assert (operator.bundle_location, operator.version, operator.created_by) == (
        location,
        1,
        None,
    )
    # Alice sees both; Bob sees only the operator's.
    assert {r["id"] for r in (await _names(client, ALICE))["orion"]} == {operator_id, mine["id"]}
    assert [r["id"] for r in (await _names(client, BOB))["orion"]] == [operator_id]


async def test_remove_is_owner_only(client, agent_store) -> None:
    mine = (await _install(client, ALICE, "orion")).json()
    operator_id = generate_agent_id()
    agent_store.create(operator_id, "shared", "test:///shared")

    assert (await client.delete(f"/v1/agents/{mine['id']}", headers=BOB)).status_code == 404
    assert (await client.delete(f"/v1/agents/{operator_id}", headers=ALICE)).status_code == 404
    assert agent_store.get(operator_id) is not None

    resp = await client.delete(f"/v1/agents/{mine['id']}", headers=ALICE)
    assert resp.status_code == 200, resp.text
    assert agent_store.get(mine["id"]) is None


async def test_builtin_names_are_reserved(client, agent_store) -> None:
    """No user can install a second "polly" beside the seeded built-in."""
    agent_store.create(builtin_agent_id("polly"), "polly", "test:///polly")
    resp = await _install(client, ALICE, "polly")
    assert resp.status_code == 409, resp.text
    assert "polly" in resp.json()["error"]
    assert [r["id"] for r in (await _names(client, ALICE))["polly"]] == [builtin_agent_id("polly")]


async def test_install_without_bundle_is_422(client) -> None:
    resp = await client.post("/v1/agents", headers=ALICE, files={"other": ("x", b"x")})
    assert resp.status_code == 422


async def test_auth_less_server_cannot_replace_a_seeded_builtin(
    agent_store, artifact_store, tmp_path
) -> None:
    """With no identity, only non-seeded unowned rows are replaceable."""
    agent_store.create(builtin_agent_id("polly"), "polly", "test:///polly")
    app = _app(agent_store, artifact_store, tmp_path, auth_provider=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await _install(c, {}, "polly")
    assert resp.status_code == 409, resp.text
    assert agent_store.get(builtin_agent_id("polly")).bundle_location == "test:///polly"


def test_template_visibility_gate() -> None:
    """Binding another user's installed agent 404s; everything else passes."""
    owned = SimpleNamespace(id="ag1", session_id=None, created_by="alice@example.com")
    require_template_visible(owned, "alice@example.com")
    with pytest.raises(OmnigentError):
        require_template_visible(owned, "bob@example.com")
    require_template_visible(SimpleNamespace(id="ag2", session_id=None, created_by=None), "bob")
    require_template_visible(SimpleNamespace(id="ag3", session_id="conv", created_by="a"), "b")


def test_only_unowned_templates_expand_server_env() -> None:
    """User-installed templates are tenant input: no ``${VAR}`` expansion."""
    base = {"id": "ag", "created_at": 0, "name": "n", "bundle_location": "x"}
    assert Agent(**base).operator_authored
    assert not Agent(**base, created_by="alice@example.com").operator_authored
    assert not Agent(**base, session_id="conv_1").operator_authored
