import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from supabase import Client, create_client

router = APIRouter()

# --- Environment Configurations ---
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """Cryptographically verifies the GitHub Webhook HMAC SHA-256 signature."""
    if not signature_header:
        return False
        
    hash_object = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256
    )
    expected_signature = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected_signature, signature_header)


async def dispatch_github_action(owner: str, repo: str, run_id: str, pr_number: int, action: str):
    """
    Fires an asynchronous HTTP POST to the GitHub Repository Dispatch API 
    to trigger the heavy Python 3.12 LangGraph orchestration worker.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {GITHUB_PAT}",
    }
    data = {
        "event_type": "pragma_review",
        "client_payload": {
            "run_id": run_id,
            "pr_number": pr_number,
            "repository": f"{owner}/{repo}",
            "action": action
        }
    }
    
    # Execute async POST request
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=data, headers=headers, timeout=5.0)
        except Exception as e:
            print(f"Failed to trigger repository dispatch: {e}")


import asyncio

async def log_payload_to_supabase_async(data_dict: dict):
    """Logs the raw incoming webhook to Supabase asynchronously using asyncio.to_thread."""
    try:
        await asyncio.to_thread(supabase.table("github_webhook_payloads").insert(data_dict).execute)
    except Exception as e:
        print(f"Failed to log payload to Supabase: {e}")


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(None)
):
    """
    Vercel Ingestion Endpoint (Serverless safe).
    Validates, logs, and dispatches the worker sequentially before returning 202 Accepted.
    """
    # 1. Read and verify payload signature
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed JSON payload")

    event_type = request.headers.get("x-github-event", "")
    
    # 2. Filter for actionable Pull Request events
    if event_type == "pull_request":
        action = payload.get("action")
        if action in ["opened", "synchronize"]:
            
            # 3. Generate context variables
            run_id = str(uuid.uuid4())
            pr_number = payload.get("pull_request", {}).get("number")
            repo_full_name = payload.get("repository", {}).get("full_name", "")
            
            if "/" in repo_full_name:
                owner, repo_name = repo_full_name.split("/", 1)
            else:
                owner, repo_name = "", repo_full_name
            
            # 4. Sequentially await serverless-safe async operations
            db_record = {
                "run_id": run_id,
                "repository": repo_full_name,
                "payload": payload,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending"
            }
            await log_payload_to_supabase_async(db_record)
            await dispatch_github_action(owner, repo_name, run_id, pr_number, action)
            
            # 5. Guaranteed 202 Return after synchronous execution completion
            return JSONResponse(status_code=202, content={"message": "Accepted, dispatching worker.", "run_id": run_id})

    # Return 202 for ignored events to satisfy GitHub deliveries
    return JSONResponse(status_code=202, content={"message": "Event ignored."})


# --- Vercel App Wrapper ---
from fastapi import FastAPI
app = FastAPI(title="PRAGMA Webhook Receiver")
app.include_router(router)
