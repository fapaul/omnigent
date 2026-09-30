"""Agent store — manages registered agents."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod

from omnigent.entities import Agent, PagedList


class AgentStore(ABC):
    """
    Abstract base for agent persistence.

    Manages the lifecycle of registered template agents: creation
    with template-name uniqueness enforcement, lookup by ID or name,
    paginated listing, and deletion.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the agent store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///agents.db"`` for SQLAlchemy or a
            filesystem path for file-backed stores.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
        created_by: str | None = None,
    ) -> Agent:
        """
        Register a new template agent. Name must be unique among
        the owner's template agents and raises if that owner already
        has a template with that name.

        :param agent_id: Pre-generated unique agent identifier,
            e.g. ``"ag_0f1a2b3c..."``. Caller generates this so
            the bundle location can be computed before persisting.
        :param name: Human-readable agent name. Must be unique
            among template agents, e.g. ``"code-assistant"``.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param description: Optional free-text description of the
            agent's purpose.
        :param created_by: Installing user for a user-owned template
            (``omnigent agent add``); ``None`` for an operator template
            visible to everyone.
        :returns: The newly created :class:`Agent`.
        """
        ...

    @abstractmethod
    def get(self, agent_id: str) -> Agent | None:
        """
        Return the agent, or ``None`` if it does not exist.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def get_by_name(self, name: str, created_by: str | None = None) -> Agent | None:
        """
        Look up a registered template agent by name within one owner.

        :param name: The template agent's name, e.g. ``"code-assistant"``.
        :param created_by: Owner to match exactly; ``None`` matches only
            unowned (operator) templates.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        viewer: str | None = None,
    ) -> PagedList[Agent]:
        """
        List the template agents *viewer* can see, with cursor pagination.

        That is every unowned (operator) template plus the templates
        *viewer* installed; other users' templates are never returned.

        ``order`` controls the sort direction on ``created_at``
        (``"desc"`` = newest-first, ``"asc"`` = oldest-first).

        :param limit: Maximum number of agents to return.
        :param after: Cursor agent ID; only return agents appearing
            *after* this agent in the sort order,
            e.g. ``"agent_abc123"``.
        :param before: Cursor agent ID; only return agents appearing
            *before* this agent in the sort order.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param viewer: Caller whose own templates are included; ``None``
            lists only unowned templates.
        :returns: A :class:`PagedList` of :class:`Agent` objects.
        """
        ...

    @abstractmethod
    def get_names(self, agent_ids: builtins.list[str]) -> dict[str, str]:
        """
        Batch-fetch agent names for a list of IDs.

        Returns a mapping from agent ID to agent name. IDs that do not
        exist in the store are silently omitted from the result.

        :param agent_ids: List of agent identifiers to look up,
            e.g. ``["ag_abc123", "ag_def456"]``.
        :returns: Mapping of ``{agent_id: agent_name}`` for found
            agents.
        """
        ...

    @abstractmethod
    def update(
        self,
        agent_id: str,
        bundle_location: str,
        created_by: str | None = None,
    ) -> Agent | None:
        """
        Update an agent's bundle location, bump its version, and
        set ``updated_at``. Returns the updated agent, or ``None``
        if no agent with the given ID exists.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :param bundle_location: New artifact store key for the
            bundle, e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param created_by: When set, stamps the agent's owner only if
            it is not already recorded (claim-on-write). Used by the
            session-scoped mutation routes to heal pre-migration rows
            whose ``created_by`` is ``None``; template-refresh callers
            leave it ``None`` so template rows stay unowned.
        :returns: The updated :class:`Agent`, or ``None`` if not
            found.
        """
        ...

    @abstractmethod
    def delete(self, agent_id: str) -> bool:
        """
        Delete an agent. Returns ``True`` if the agent existed,
        ``False`` otherwise. Caller is responsible for cancelling
        in-flight tasks before calling this.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: ``True`` if the agent was deleted, ``False`` if
            it did not exist.
        """
        ...
