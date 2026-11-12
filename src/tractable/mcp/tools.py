"""The small agent surface over shared service operations.

``list_items`` / ``show_item`` / ``create_item`` / ``register_batch`` /
``update_item`` / ``take`` / ``prepare`` / ``bind_batch_pr`` /
``advance_batch_head`` / ``recover_batch_owner`` / ``show_batch`` /
``abort_batch`` / ``release`` / ``finish`` / ``renew`` /
``list_deferrals`` / ``defer`` / ``promote_deferral`` /
``dismiss_deferral`` are backed by :mod:`tractable.service`, the same
operations the human/CI CLI uses. Blocking Git work runs via
``asyncio.to_thread``; there is no shared mutable connection to guard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..errors import E_NO_PRINCIPAL, E_STATE
from ..service import TrackerService, err, resolve_payload
from .identity import PrincipalHolder, sanitize_client_name
from .target import ResolvedTarget

LOG = logging.getLogger("tractable.mcp")


def _principal(holder: PrincipalHolder, ctx: Context | None) -> str | None:
    if holder.principal is not None:
        return holder.principal
    try:
        ci = ctx.request_context.session.client_params.clientInfo if ctx else None  # type: ignore[union-attr]
    except AttributeError:
        ci = None
    if ci is not None:
        return holder.ensure(ci)
    return holder.principal


def _client_name(ctx: Context | None) -> str | None:
    """Best-effort harness label for session-history attribution."""
    try:
        ci = ctx.request_context.session.client_params.clientInfo if ctx else None  # type: ignore[union-attr]
    except AttributeError:
        ci = None
    return sanitize_client_name(getattr(ci, "name", None))


def _service(
    target: ResolvedTarget, worker: str, holder: PrincipalHolder, ctx: Context | None,
) -> TrackerService:
    return TrackerService(
        ref=target.state_ref,
        cache_dir=target.cache_dir,
        worker=worker,
        session_id=holder.session_id,
        client_name=_client_name(ctx),
    )


def _need_principal(holder: PrincipalHolder, ctx: Context | None) -> str | dict[str, Any] | None:
    worker = _principal(holder, ctx)
    if not worker:
        return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")
    return worker


def register_tools(server: FastMCP, target: ResolvedTarget, holder: PrincipalHolder) -> None:
    @server.tool(
        name="list_items",
        description="List task summaries with filters and stable pagination.",
    )
    async def list_items_tool(
        status: str | None = None,
        priority: str | None = None,
        text: str | None = None,
        ready_only: bool = False,
        limit: int = 5,
        cursor: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.list_items, status=status, priority=priority, text=text,
            ready_only=ready_only, limit=limit, cursor=cursor,
        )

    @server.tool(
        name="show_item",
        description="Show a task or one paged large field.",
    )
    async def show_item_tool(
        id: str,
        field: str | None = None,
        offset: int = 0,
        budget: int = 6000,
        rev: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.show_item, id, field=field, offset=offset, budget=budget, rev=rev)

    @server.tool(
        name="create_item",
        description="Create a task, optionally as a registered batch member.",
    )
    async def create_item_tool(
        id: str,
        title: str,
        priority: str = "medium",
        description: str = "",
        needs: list[str] | None = None,
        acceptance: list[str] | None = None,
        links: list[str] | None = None,
        context: str = "",
        batch: dict[str, Any] | None = None,
        not_before: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.create_item, id, title, priority=priority, description=description,
            needs=needs or [], acceptance=acceptance or [], links=links or [], context=context, batch=batch,
            not_before=not_before,
        )

    @server.tool(
        name="register_batch",
        description=(
            "Register an immutable batch. The first registration on a store below schema 4 needs "
            "confirm_schema3_cutover=true (one cutover to schema 4). develop_ref (refs/remotes/...) "
            "is fixed here and required for later develop-merge advances."
        ),
    )
    async def register_batch_tool(
        batch_id: str,
        project_id: str,
        repository: str,
        owner_generation: str,
        integration_branch: str,
        integration_worktree: str,
        start_head: str,
        members: list[str],
        scope: dict[str, list[str]],
        scope_hash: str,
        delivery_boundary: str,
        terminal_outcome: str,
        develop_ref: str | None = None,
        confirm_schema3_cutover: bool = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.register_batch,
            batch_id=batch_id, project_id=project_id, repository=repository,
            owner_generation=owner_generation, integration_branch=integration_branch,
            integration_worktree=integration_worktree, start_head=start_head,
            members=members, scope=scope, scope_hash=scope_hash,
            delivery_boundary=delivery_boundary, terminal_outcome=terminal_outcome,
            develop_ref=develop_ref, confirm_schema3_cutover=confirm_schema3_cutover,
        )

    @server.tool(
        name="update_item",
        description=(
            "Edit task fields or move between open and blocked. Text and list fields are replaced "
            "wholesale; an empty value clears one, and not_before=\"\" or clear_not_before=true clears a hold. "
            "context_append "
            "adds a note to the end of the current context and keeps concurrent notes; not with context."
        ),
    )
    async def update_item_tool(
        id: str,
        title: str | None = None,
        priority: str | None = None,
        description: str | None = None,
        needs: list[str] | None = None,
        acceptance: list[str] | None = None,
        links: list[str] | None = None,
        context: str | None = None,
        context_append: str | None = None,
        batch: dict[str, Any] | None = None,
        status: str | None = None,
        not_before: str | None = None,
        clear_not_before: bool = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        if clear_not_before and not_before:
            return err(E_STATE, "pass not_before or clear_not_before, not both")
        if clear_not_before:
            not_before = ""
        svc = _service(target, worker, holder, ctx)
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if priority is not None:
            kwargs["priority"] = priority
        if description is not None:
            kwargs["description"] = description
        if needs is not None:
            kwargs["needs"] = needs
        if acceptance is not None:
            kwargs["acceptance"] = acceptance
        if links is not None:
            kwargs["links"] = links
        if context is not None:
            kwargs["context"] = context
        if context_append is not None:
            kwargs["context_append"] = context_append
        if batch is not None:
            kwargs["batch"] = batch
        if status is not None:
            kwargs["status"] = status
        if not_before is not None:
            kwargs["not_before"] = not_before
        return await asyncio.to_thread(svc.update_item, id, **kwargs)

    @server.tool(
        name="take",
        description=(
            "Claim one ready task and return its generation and context. Members of an active "
            "batch are reserved to the batch owner (E_BATCH_OWNER)."
        ),
    )
    async def take_tool(
        id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.take, id)

    @server.tool(
        name="takeover",
        description=(
            "Take over a live claim held by a session that cannot resume. "
            "Name the exact holder you inspected and why it is dead; the "
            "transfer is audited under your session."
        ),
    )
    async def takeover_tool(
        id: str,
        expected_holder: str,
        reason: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.takeover, id, expected_holder, reason)

    @server.tool(
        name="release",
        description="Release a claim by generation without finishing.",
    )
    async def release_tool(
        id: str,
        generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.release, id, generation)

    @server.tool(
        name="prepare",
        description=(
            "Record verified batch-member work without completing the task. source_base must be "
            "the batch start head or a recorded integration head. verification may be passed inline or as "
            "verification_path, an absolute path to a JSON file holding it."
        ),
    )
    async def prepare_tool(
        id: str,
        generation: str,
        batch_id: str,
        member_id: str,
        source_worktree: str,
        source_revision: str,
        source_base: str,
        accepted_head: str,
        integration_head: str,
        scope_hash: str,
        verification: dict[str, Any] | None = None,
        verification_path: str | None = None,
        implementation_dependencies: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        verification, refusal = await asyncio.to_thread(resolve_payload, "verification", verification, verification_path)
        if refusal is not None:
            return refusal
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.prepare,
            id,
            generation,
            batch_id=batch_id,
            member_id=member_id,
            source_worktree=source_worktree,
            source_revision=source_revision,
            source_base=source_base,
            accepted_head=accepted_head,
            integration_head=integration_head,
            scope_hash=scope_hash,
            verification=verification,
            implementation_dependencies=implementation_dependencies or [],
        )

    @server.tool(
        name="bind_batch_pr",
        description=(
            "Bind the batch's final PR, or move its head with expected_previous_head. Same "
            "repository, number and node id only; an exact retry is idempotent."
        ),
    )
    async def bind_batch_pr_tool(
        batch_id: str,
        owner_generation: str,
        pr_repository: str,
        number: int,
        node_id: str,
        head: str,
        expected_previous_head: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.bind_batch_pr, batch_id, owner_generation, pr_repository=pr_repository, number=number,
            node_id=node_id, head=head, expected_previous_head=expected_previous_head,
        )

    @server.tool(
        name="advance_batch_head",
        description=(
            "Owner-only: record a leader fix or develop merge as the new integration head. "
            "expected_head is compare-and-swap; reason is fix or develop-merge."
        ),
    )
    async def advance_batch_head_tool(
        batch_id: str,
        owner_generation: str,
        expected_head: str,
        head: str,
        reason: str,
        changed_files: list[str],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.advance_batch_head, batch_id, owner_generation, expected_head=expected_head,
            head=head, reason=reason, changed_files=changed_files,
        )

    @server.tool(
        name="recover_batch_owner",
        description=(
            "Take batch ownership after the leader died. Name the exact owner and generation you "
            "inspected, a new generation you generated and stored, and why the owner cannot resume. "
            "Pass expected_integration_worktree, expected_integration_head, and integration_worktree "
            "to relocate the integration checkout."
        ),
    )
    async def recover_batch_owner_tool(
        batch_id: str,
        expected_owner: str,
        expected_owner_generation: str,
        new_owner_generation: str,
        reason: str,
        expected_integration_worktree: str | None = None,
        expected_integration_head: str | None = None,
        integration_worktree: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.recover_batch_owner, batch_id, expected_owner, expected_owner_generation,
            new_owner_generation, reason,
            expected_integration_worktree=expected_integration_worktree,
            expected_integration_head=expected_integration_head,
            integration_worktree=integration_worktree,
        )
    @server.tool(
        name="show_batch",
        description="Show a batch registry record, or one paged section (members, receipts, integration_log, ...).",
    )
    async def show_batch_tool(
        batch_id: str,
        field: str | None = None,
        offset: int = 0,
        budget: int = 6000,
        rev: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.show_batch, batch_id, field=field, offset=offset, budget=budget, rev=rev)

    @server.tool(
        name="abort_batch",
        description="Abort a claim-free active batch and recover its members.",
    )
    async def abort_batch_tool(
        batch_id: str,
        owner_generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.abort_batch, batch_id, owner_generation)

    @server.tool(
        name="finish",
        description=(
            "Close a task. Prepared tasks additionally require current final-tree evidence, "
            "inline as final_evidence or as final_evidence_path, an absolute path to a JSON file."
        ),
    )
    async def finish_tool(
        id: str,
        generation: str,
        final_evidence: dict[str, Any] | None = None,
        final_evidence_path: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        if final_evidence is not None or final_evidence_path is not None:
            final_evidence, refusal = await asyncio.to_thread(
                resolve_payload, "final_evidence", final_evidence, final_evidence_path,
            )
            if refusal is not None:
                return refusal
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.finish, id, generation, final_evidence)

    @server.tool(
        name="renew",
        description="Extend a long-running claim. Same generation, no progress milestones required.",
    )
    async def renew_tool(
        id: str,
        generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.renew, id, generation)

    @server.tool(
        name="drop",
        description=(
            "Abandon a task as dropped, recording why. reason is required (non-empty, 2000 characters at "
            "most, newlines allowed) and is stored with the status change in one commit. "
            "Unclaimed tasks drop freely; a live claim needs its generation."
        ),
    )
    async def drop_tool(
        id: str,
        reason: str,
        generation: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.drop, id, reason=reason, generation=generation)

    @server.tool(
        name="list_deferrals",
        description="List deferral rows: open by default, all for every resolution.",
    )
    async def list_deferrals_tool(
        from_item: str | None = None,
        resolution: str | None = None,
        limit: int = 5,
        cursor: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.list_deferrals, from_item=from_item, resolution=resolution,
            limit=limit, cursor=cursor,
        )

    @server.tool(
        name="defer",
        description=(
            "Record deferred work on a task without changing its lifecycle. summary is one line, "
            "200 characters at most; done or dropped tasks refuse deferrals."
        ),
    )
    async def defer_tool(
        id: str,
        summary: str,
        reason: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.defer_item, id, summary, reason)

    @server.tool(
        name="promote_deferral",
        description=(
            "Resolve an open deferral into a linked planning item. "
            "Creates the successor from the deferral by default; pass "
            "to_item to link an existing item, or title/priority to shape "
            "the created one. For full successor control, create the item "
            "first and link it with to_item."
        ),
    )
    async def promote_deferral_tool(
        id: str,
        deferral_id: int,
        to_item: str | None = None,
        title: str | None = None,
        priority: str = "medium",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.promote_deferral,
            id,
            deferral_id,
            to_item=to_item,
            title=title,
            priority=priority,
        )

    @server.tool(
        name="dismiss_deferral",
        description="Resolve an open deferral as deliberately dropped, with a reason.",
    )
    async def dismiss_deferral_tool(
        id: str,
        deferral_id: int,
        reason: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.dismiss_deferral, id, deferral_id, reason)
