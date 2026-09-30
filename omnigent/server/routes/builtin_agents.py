"""Read-only route for discovering built-in agents (``GET /v1/agents``).

Built-in agents are the long-lived, shared agents the server provides
out of the box — the seeded ``claude-native-ui`` agent plus anything
registered at startup with ``omnigent server --agent``. They are the
``session_id IS NULL`` rows in ``agent_store``; ``agent_store.list()``
already filters to exactly these. Session-scoped agents (created via
multipart ``POST /v1/sessions``) belong to one conversation and are read
through ``GET /v1/sessions/{id}/agent`` — never here.

The Web UI's new-session picker calls this to discover bindable
built-ins, then creates a session with
``POST /v1/sessions {agent_id, host_id, workspace}``. See
``designs/BUILTIN_AGENTS.md``.

Users also install their own reusable agents here (``omnigent agent add``):
``POST /v1/agents`` stores a template stamped with the caller as
``created_by``, and ``DELETE /v1/agents/{id}`` removes one. The list returns
operator templates plus the caller's own, never another user's.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.datastructures import UploadFile

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.schemas import AgentObject, MCPServerSummary, PaginatedList, SkillSummary
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

_logger = logging.getLogger(__name__)


def _to_agent_object(agent: Agent, agent_cache: AgentCache) -> AgentObject:
    """
    Convert a runtime Agent entity to an API-layer AgentObject.

    Loads the spec from cache to populate ``mcp_servers``,
    ``skills``, and (when the stored row has none) the
    ``description``; on any load failure those fall back to empty /
    the stored value rather than failing the whole list — one
    unreadable bundle must not break discovery.

    :param agent: The runtime agent entity, e.g. the seeded
        ``claude-native-ui`` agent.
    :param agent_cache: Cache used to load the agent spec.
    :returns: An :class:`AgentObject` for the API response.
    """
    mcp_servers: list[MCPServerSummary] = []
    skills: list[SkillSummary] = []
    terminals: list[str] = []
    harness: str | None = None
    # Prefer the stored entity's description; fall back to the spec's
    # top-level description when the stored value is unset (single-file
    # YAML agents don't persist it at registration today). Lets the
    # new-session picker show a hover description without a migration.
    description: str | None = agent.description
    try:
        # Built-ins are operator-authored template agents
        # (session_id is None), so ${VAR} expansion against the server
        # env is allowed here; a tenant session-scoped agent would not
        # expand.
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
        if description is None:
            description = loaded.spec.description
        # Declared terminal names, in spec order (mirrors the
        # session-agent endpoint so both report it consistently).
        terminals = list(loaded.spec.terminals or {})
        # Bundled suggestions stay available while the host catalog loads.
        skills = [
            SkillSummary(name=s.name, description=s.description)
            for s in loaded.spec.skills
            if s.user_invocable
        ]
        mcp_servers = [
            MCPServerSummary(
                name=srv.name,
                transport=srv.transport,
                description=srv.description,
                url=srv.url,
                headers=dict.fromkeys(srv.headers, "[REDACTED]") if srv.headers else {},
                command=srv.command,
                args=srv.args,
            )
            for srv in loaded.spec.mcp_servers
        ]
        # Kind for the Add Agent picker (Codex vs Claude). Stays None
        # when the bundle can't be loaded (the except below).
        harness = loaded.spec.executor.harness_kind
    except Exception:  # noqa: BLE001 — spec load failure must not break the list
        _logger.debug(
            "Failed to load spec for agent %s; mcp_servers/skills will be empty",
            agent.id,
            exc_info=True,
        )
    return AgentObject(
        id=agent.id,
        name=agent.name,
        version=agent.version,
        description=description,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        harness=harness,
        mcp_servers=mcp_servers,
        mcp_servers_editable=False,
        skills=skills,
        terminals=terminals,
        # Seeded built-ins use a deterministic, name-derived id; an
        # operator/user-registered template (e.g. ``--agent``) uses a
        # random id. The picker protects the former from being shadowed
        # by a same-named ``omnigent run`` upload, but lets a newer
        # upload supersede the latter.
        builtin=agent.session_id is None and agent.id == builtin_agent_id(agent.name),
        installed=agent.session_id is None and agent.created_by is not None,
    )


def install_user_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: AgentCache,
    *,
    owner: str | None,
    name: str,
    description: str | None,
    bundle_bytes: bytes,
) -> Agent:
    """
    Create or replace *owner*'s template named *name* from a validated bundle.

    Only a row matching owner AND name is ever replaced, in place (stable
    ``agent_id``, bumped ``version``); anything else gets a new row. On a
    multi-user server *owner* is always a real user id (``require_user``
    401s otherwise), so an unowned operator template can never match and
    be overwritten. ``owner=None`` happens only without auth, where the
    local user is the operator. A seeded built-in's name is reserved for
    everyone, so no install can appear as a second "Polly" in the picker.

    :param owner: Installing user, or ``None`` on an auth-less server.
    :param name: Agent name from the bundle's spec, e.g. ``"orion"``.
    :param description: Description from the spec, if any.
    :param bundle_bytes: Gzipped tarball already checked by
        :func:`validate_agent_bundle`.
    :returns: The created or updated template agent.
    :raises OmnigentError: ``CONFLICT`` when the name belongs to a seeded
        built-in.
    """
    if agent_store.get(builtin_agent_id(name)) is not None:
        raise OmnigentError(
            f"{name!r} is a built-in agent; rename your agent to install it.",
            code=ErrorCode.CONFLICT,
        )
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    existing = agent_store.get_by_name(name, created_by=owner)
    if existing is None:
        agent_id = generate_agent_id()
        location = f"{agent_id}/{bundle_hash}"
        artifact_store.put(location, bundle_bytes)
        return agent_store.create(agent_id, name, location, description, created_by=owner)
    location = f"{existing.id}/{bundle_hash}"
    artifact_store.put(location, bundle_bytes)
    if existing.bundle_location == location:
        return existing
    updated = agent_store.update(existing.id, location)
    agent_cache.evict(existing.id)
    if updated is None:  # deleted concurrently
        raise OmnigentError(f"Agent not found: {existing.id!r}", code=ErrorCode.NOT_FOUND)
    return updated


def create_builtin_agents_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    *,
    artifact_store: ArtifactStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``GET /v1/agents`` (built-in discovery).

    Mounted with ``prefix="/v1"`` so the final path is ``/v1/agents``.

    :param agent_store: Store whose ``list()`` returns only built-in
        (``session_id IS NULL``) agents.
    :param agent_cache: Cache for loading specs (populates
        ``mcp_servers`` on each agent).
    :param artifact_store: Bundle store for installs; ``None`` disables
        ``POST /v1/agents``.
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated.
    :returns: A FastAPI router exposing the list, install, and remove routes.
    """
    router = APIRouter()

    @router.get("/agents")
    async def list_builtin_agents(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """List built-in agents with cursor-based pagination.

        Returns only built-in agents — ``agent_store.list()`` filters
        ``session_id IS NULL`` — so session-scoped agents never appear.

        :param request: The incoming FastAPI request (for auth).
        :param limit: Maximum number of agents to return (1-1000).
        :param after: Cursor — return agents after this id.
        :param before: Cursor — return agents before this id.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: A :class:`PaginatedList` of built-in agents.
        """
        user_id = _require_user(request, auth_provider)
        page = agent_store.list(
            limit=limit, after=after, before=before, order=order, viewer=user_id
        )
        return PaginatedList(
            data=[_to_agent_object(a, agent_cache) for a in page.data],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    @router.post("/agents")
    async def install_agent(request: Request) -> AgentObject:
        """Install (or replace) the caller's reusable agent from a bundle.

        Multipart form with one ``bundle`` part: a gzipped tarball, the same
        shape multipart ``POST /v1/sessions`` accepts. The bundle is validated
        like any tenant upload and never executed here.

        :param request: The incoming request carrying the ``bundle`` part.
        :returns: The installed agent as an :class:`AgentObject`.
        """
        user_id = _require_user(request, auth_provider)
        if artifact_store is None:
            raise OmnigentError("artifact store is not configured", code=ErrorCode.INTERNAL_ERROR)
        bundle = (await request.form()).get("bundle")
        if not isinstance(bundle, UploadFile):
            raise HTTPException(status_code=422, detail="multipart part 'bundle' is required")
        bundle_bytes = await bundle.read()
        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        if spec.name is None:  # validate_agent_bundle rejects this; narrows the type
            raise OmnigentError("agent spec has no name", code=ErrorCode.INVALID_INPUT)
        agent = await asyncio.to_thread(
            install_user_agent,
            agent_store,
            artifact_store,
            agent_cache,
            owner=user_id,
            name=spec.name,
            description=spec.description,
            bundle_bytes=bundle_bytes,
        )
        return _to_agent_object(agent, agent_cache)

    @router.delete("/agents/{agent_id}")
    async def remove_agent(request: Request, agent_id: str) -> dict[str, object]:
        """Remove one of the caller's installed agents.

        Sessions already started from it keep running: each holds its own
        session-scoped copy. Another user's agent, a session-scoped agent,
        and (on a multi-user server) any operator template all read as 404.

        :param request: The incoming request (for auth).
        :param agent_id: Template agent to remove.
        :returns: ``{"id": agent_id, "deleted": True}``.
        """
        user_id = _require_user(request, auth_provider)
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        if agent is None or agent.session_id is not None or agent.created_by != user_id:
            raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
        if agent.id == builtin_agent_id(agent.name):
            raise OmnigentError("Built-in agents cannot be removed.", code=ErrorCode.INVALID_INPUT)
        await asyncio.to_thread(agent_store.delete, agent_id)
        agent_cache.evict(agent_id)
        return {"id": agent_id, "deleted": True}

    return router
