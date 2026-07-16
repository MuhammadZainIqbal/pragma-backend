import asyncio
import json
import os
from typing import List

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import Client, create_client

from app.graphs.state import AgentFinding

router = APIRouter(prefix="/api")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


class ApprovePayload(BaseModel):
    """Request body for the HITL approve endpoint."""
    findings: List[AgentFinding]


async def _dispatch_resume(run_id: str, pr_number: int, repo: str, edits_json: str) -> None:
    """
    Fire a GitHub Repository Dispatch event with event_type: pragma_resume,
    passing the run_id, mode, and human-edited findings for the resume worker to consume.
    """
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {GITHUB_PAT}",
    }
    payload = {
        "event_type": "pragma_resume",
        "client_payload": {
            "run_id": run_id,
            "pr_number": pr_number,
            "repository": repo,
            "mode": "resume",
            "edits": edits_json,
        }
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=5.0)
            resp.raise_for_status()
        except Exception as e:
            print(f"ERROR: Failed to dispatch pragma_resume for run_id={run_id}: {e}")


@router.get("/reviews/{run_id}")
async def get_review(run_id: str):
    """
    GET /api/reviews/{run_id}
    Returns the current findings and metadata for a given run from Supabase.
    Used by the React dashboard to hydrate the review panel.
    """
    try:
        result = await asyncio.to_thread(
            supabase.table("github_webhook_payloads")
            .select("run_id, repository, status, payload, created_at")
            .eq("run_id", run_id)
            .single()
            .execute
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase query failed: {e}")

    if not result.data:
        raise HTTPException(status_code=404, detail=f"No review found for run_id: {run_id}")

    return JSONResponse(status_code=200, content=result.data)


@router.patch("/reviews/{run_id}/approve")
async def approve_review(run_id: str, body: ApprovePayload):
    """
    PATCH /api/reviews/{run_id}/approve
    Accepts human-edited findings from the dashboard client.
    Persists the edits to Supabase and fires a pragma_resume dispatch
    to resume the interrupted LangGraph execution from the HITL gate.
    """
    # 1. Validate review exists
    try:
        existing = await asyncio.to_thread(
            supabase.table("github_webhook_payloads")
            .select("run_id, payload")
            .eq("run_id", run_id)
            .single()
            .execute
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase query failed: {e}")

    if not existing.data:
        raise HTTPException(status_code=404, detail=f"No review found for run_id: {run_id}")

    # 2. Serialize human findings to a JSON string for the dispatch payload
    edits_json = json.dumps([f.model_dump() for f in body.findings])

    # 3. Persist edits and update status to 'resuming' in Supabase for dashboard observability
    try:
        await asyncio.to_thread(
            supabase.table("github_webhook_payloads")
            .update({"status": "resuming", "human_edits": json.loads(edits_json)})
            .eq("run_id", run_id)
            .execute
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist HITL edits: {e}")

    # 4. Extract PR context from stored payload to pass into dispatch
    pr_number = existing.data.get("payload", {}).get("pull_request", {}).get("number", 0)
    repo = existing.data.get("repository", "")

    # 5. Fire async GitHub dispatch — triggers the GitHub Actions resume worker
    await _dispatch_resume(run_id, pr_number, repo, edits_json)

    return JSONResponse(
        status_code=202,
        content={"message": "HITL edits accepted. Resuming PRAGMA review pipeline.", "run_id": run_id}
    )
