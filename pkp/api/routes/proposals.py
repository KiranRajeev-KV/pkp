"""Proposal API routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from pkp.api.deps import get_db
from pkp.storage.db import Database, db_context
from pkp.storage.models import ProposalWithDocuments

router = APIRouter()
logger = logging.getLogger(__name__)


class ProposalResponse(BaseModel):
    """Proposal response with document display metadata."""

    proposal_id: str
    doc_a_sha256: str
    doc_b_sha256: str
    score: float
    rationale: str | None
    status: str
    created_at: datetime
    reviewed_at: datetime | None
    link_type: str | None
    doc_a_title: str | None
    doc_a_url: str | None
    doc_b_title: str | None
    doc_b_url: str | None


class ProposalListResponse(BaseModel):
    """Proposal list response."""

    status: str
    limit: int
    results: list[ProposalResponse]
    total: int


class ApproveProposalRequest(BaseModel):
    """Request to approve a proposal."""

    link_type: str | None = None


def _proposal_response(proposal: ProposalWithDocuments) -> ProposalResponse:
    """Build an API response from a joined proposal record."""
    return ProposalResponse(
        proposal_id=proposal.proposal_id,
        doc_a_sha256=proposal.doc_a_sha256,
        doc_b_sha256=proposal.doc_b_sha256,
        score=proposal.score,
        rationale=proposal.rationale,
        status=proposal.status,
        created_at=proposal.created_at,
        reviewed_at=proposal.reviewed_at,
        link_type=proposal.link_type,
        doc_a_title=proposal.doc_a_title,
        doc_a_url=proposal.doc_a_url,
        doc_b_title=proposal.doc_b_title,
        doc_b_url=proposal.doc_b_url,
    )


@router.get("/proposals", response_model=ProposalListResponse)
async def list_proposals(
    db: Annotated[Database, Depends(get_db)],
    status: str = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ProposalListResponse:
    """List proposals by status."""
    proposals = await db.get_proposals_with_documents_by_status(status, limit)
    results = [_proposal_response(proposal) for proposal in proposals]
    return ProposalListResponse(
        status=status,
        limit=limit,
        results=results,
        total=len(results),
    )


@router.get("/proposals/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(
    proposal_id: str,
    db: Annotated[Database, Depends(get_db)],
) -> ProposalResponse:
    """Get one proposal by proposal_id."""
    proposal = await db.get_proposal_with_documents(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _proposal_response(proposal)


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalResponse)
async def approve_proposal(
    proposal_id: str,
    request: ApproveProposalRequest | None = None,
) -> ProposalResponse:
    """Approve a pending proposal."""
    async with db_context() as db:
        proposal = await db.get_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if proposal.status != "pending":
            raise HTTPException(status_code=409, detail="Proposal is not pending")

        reviewed_at = datetime.now(UTC)
        link_type = request.link_type if request else None
        approved = await db.approve_proposal(proposal_id, link_type, reviewed_at)
        if not approved:
            raise HTTPException(status_code=409, detail="Proposal is not pending")

        # Future VaultWriter integration point: write approved links to the vault here.
        logger.info("vault writer stub proposal_id=%s", proposal_id)

        updated = await db.get_proposal_with_documents(proposal_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return _proposal_response(updated)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalResponse)
async def reject_proposal(proposal_id: str) -> ProposalResponse:
    """Reject a pending proposal."""
    async with db_context() as db:
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

        updated = await db.get_proposal_with_documents(proposal_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return _proposal_response(updated)
