"""Review queue UI routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from pkp import __version__
from pkp.api.deps import get_db
from pkp.storage.db import Database
from pkp.storage.models import ProposalWithDocuments

router = APIRouter()

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
LINK_TYPES = ("related", "extends", "contradicts", "prerequisite")


def _score_percent(score: float) -> int:
    """Convert a proposal score into a bounded display width."""
    return round(max(0.0, min(score, 1.0)) * 100)


def _score_tone(score: float) -> str:
    """Return score color treatment."""
    if score >= 0.85:
        return "green"
    if score >= 0.7:
        return "amber"
    return "zinc"


def _score_decimal(score: float) -> str:
    """Format a proposal score without implying confidence."""
    return f"{score:.3f}"


templates.env.filters["score_percent"] = _score_percent
templates.env.filters["score_tone"] = _score_tone
templates.env.filters["score_decimal"] = _score_decimal


async def _pending_count(db: Database) -> int:
    """Count pending proposals for queue chrome."""
    row = await db._fetch_one(
        "SELECT COUNT(*) AS pending_count FROM proposals WHERE status = ?",
        ("pending",),
    )
    if row is None:
        return 0
    return int(row["pending_count"])


async def _next_pending_proposal(
    db: Database, skip_id: str | None = None
) -> ProposalWithDocuments | None:
    """Return the next pending proposal, optionally moving past one id."""
    proposals = await db.get_proposals_with_documents_by_status("pending", 100)
    if not proposals:
        return None

    if skip_id is None:
        return proposals[0]

    for index, proposal in enumerate(proposals):
        if proposal.proposal_id == skip_id:
            if index + 1 < len(proposals):
                return proposals[index + 1]
            return proposals[0]

    return proposals[0]


async def _pending_proposals(
    db: Database, limit: int = 100
) -> list[ProposalWithDocuments]:
    """Return pending proposals for batch review."""
    return await db.get_proposals_with_documents_by_status("pending", limit)


def _toast_header(message: str) -> dict[str, str]:
    """Build an HTMX trigger header for toast notifications."""
    return {"HX-Trigger": json.dumps({"queue:toast": {"message": message}})}


async def _render_proposal_slot(
    request: Request,
    db: Database,
    proposal: ProposalWithDocuments | None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render the proposal slot fragment and pending badge context."""
    pending_count = await _pending_count(db)
    template_name = (
        "queue/_proposal_card.html"
        if proposal is not None
        else "queue/_empty_state.html"
    )
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "include_badge_update": True,
            "link_types": LINK_TYPES,
            "pending_count": pending_count,
            "proposal": proposal,
        },
        headers=headers,
    )


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Render the review queue page shell."""
    pending_count = await _pending_count(db)
    proposal = await _next_pending_proposal(db)
    return templates.TemplateResponse(
        request,
        "queue/index.html",
        {
            "asset_version": __version__,
            "link_types": LINK_TYPES,
            "pending_count": pending_count,
            "proposal": proposal,
            "title": "PKP · Review Queue",
        },
    )


@router.get("/queue/pending-count", response_class=HTMLResponse)
async def pending_count_badge(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Render the pending proposal count badge."""
    pending_count = await _pending_count(db)
    return templates.TemplateResponse(
        request,
        "queue/_pending_badge.html",
        {"pending_count": pending_count},
    )


@router.get("/queue/batch", response_class=HTMLResponse)
async def batch_queue(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Render the pending proposal batch list."""
    proposals = await _pending_proposals(db)
    pending_count = await _pending_count(db)
    return templates.TemplateResponse(
        request,
        "queue/_batch.html",
        {
            "pending_count": pending_count,
            "proposals": proposals,
        },
    )


@router.get("/queue/next", response_class=HTMLResponse)
async def next_proposal(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    skip_id: str | None = None,
) -> HTMLResponse:
    """Render the next pending proposal card."""
    proposal = await _next_pending_proposal(db, skip_id)
    headers = _toast_header("Skipped — will appear again later") if skip_id else None
    return await _render_proposal_slot(request, db, proposal, headers)


@router.post("/queue/bulk/approve", response_class=HTMLResponse)
async def bulk_approve_queue_proposals(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Approve selected pending proposals and render the refreshed batch list."""
    form = await request.form()
    proposal_ids = [str(value) for value in form.getlist("proposal_ids")]
    reviewed_at = datetime.now(UTC)

    approved_count = 0
    for proposal_id in proposal_ids:
        proposal = await db.get_proposal(proposal_id)
        if proposal is None or proposal.status != "pending":
            continue
        approved = await db.approve_proposal(proposal_id, None, reviewed_at)
        if approved:
            approved_count += 1

    proposals = await _pending_proposals(db)
    pending_count = await _pending_count(db)
    message = "Connection approved and saved"
    if approved_count != 1:
        message = f"{approved_count} connections approved and saved"
    return templates.TemplateResponse(
        request,
        "queue/_batch.html",
        {
            "include_badge_update": True,
            "pending_count": pending_count,
            "proposals": proposals,
        },
        headers=_toast_header(message),
    )


@router.post("/queue/bulk/reject", response_class=HTMLResponse)
async def bulk_reject_queue_proposals(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Reject selected pending proposals and render the refreshed batch list."""
    form = await request.form()
    proposal_ids = [str(value) for value in form.getlist("proposal_ids")]

    rejected_count = 0
    for proposal_id in proposal_ids:
        proposal = await db.get_proposal(proposal_id)
        if proposal is None or proposal.status != "pending":
            continue
        rejected = await db.reject_proposal(
            proposal_id,
            proposal.doc_a_sha256,
            proposal.doc_b_sha256,
        )
        if rejected:
            rejected_count += 1

    proposals = await _pending_proposals(db)
    pending_count = await _pending_count(db)
    message = "Proposal rejected"
    if rejected_count != 1:
        message = f"{rejected_count} proposals rejected"
    return templates.TemplateResponse(
        request,
        "queue/_batch.html",
        {
            "include_badge_update": True,
            "pending_count": pending_count,
            "proposals": proposals,
        },
        headers=_toast_header(message),
    )


@router.post("/queue/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_queue_proposal(
    proposal_id: str,
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Approve a proposal and render the next card."""
    proposal = await db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="Proposal is not pending")

    form = await request.form()
    raw_link_type = form.get("link_type")
    link_type = str(raw_link_type) if raw_link_type else None
    if link_type is not None and link_type not in LINK_TYPES:
        raise HTTPException(status_code=422, detail="Invalid link_type")
    reviewed_at = datetime.now(UTC)
    approved = await db.approve_proposal(proposal_id, link_type, reviewed_at)
    if not approved:
        raise HTTPException(status_code=409, detail="Proposal is not pending")

    next_pending = await _next_pending_proposal(db)
    headers = _toast_header("Connection approved and saved")
    return await _render_proposal_slot(request, db, next_pending, headers)


@router.post("/queue/{proposal_id}/reject", response_class=HTMLResponse)
async def reject_queue_proposal(
    proposal_id: str,
    request: Request,
    db: Annotated[Database, Depends(get_db)],
) -> HTMLResponse:
    """Reject a proposal and render the next card."""
    proposal = await db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="Proposal is not pending")

    rejected = await db.reject_proposal(
        proposal_id,
        proposal.doc_a_sha256,
        proposal.doc_b_sha256,
    )
    if not rejected:
        raise HTTPException(status_code=409, detail="Proposal is not pending")

    next_pending = await _next_pending_proposal(db)
    headers = _toast_header("Proposal rejected")
    return await _render_proposal_slot(request, db, next_pending, headers)
