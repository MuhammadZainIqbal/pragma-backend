import os
import uuid
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv

# Initialize environment configurations
load_dotenv()

from app.graphs.graph import compile_pragma_graph

# Security evaluation: Guard documentation endpoints against production scanning
IS_PROD = os.getenv("ENVIRONMENT", "development").lower() == "production"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize pool with open=False, then explicitly await open
    db_url = os.environ.get("DATABASE_URL")
    app.state.db_pool = AsyncConnectionPool(conninfo=db_url, open=False)
    await app.state.db_pool.open()
    print("🚀 [PRAGMA INFO] Async Database Connection Pool Opened Successfully.")
    yield
    await app.state.db_pool.close()
    print("🛑 [PRAGMA INFO] Async Database Connection Pool Closed Cleanly.")

app = FastAPI(
    title="PRAGMA Persistent API Server",
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None if IS_PROD else "/redoc",
    openapi_url=None if IS_PROD else "/openapi.json",
    lifespan=lifespan
)

# Allow frontend requests
origins = [
    "http://localhost:5173",
    "https://pragma-frontend.vercel.app", 
    "https://pragma.usbro.dev", # Zain's custom domain
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def process_github_webhook(run_id: str, pr_number: int, full_name: str, diff_url: str, head_sha: str, pool: AsyncConnectionPool):
    print(f"🟢 [PRAGMA LOG] Ingesting webhook for PR #{pr_number}...")
    github_pat = os.environ.get("GITHUB_PAT")
    headers = {"Authorization": f"token {github_pat}"} if github_pat else {}
    
    # 1. Fetch raw diff from GitHub
    async with httpx.AsyncClient() as client:
        resp = await client.get(diff_url, headers=headers, follow_redirects=True)
        if resp.status_code != 200:
            print(f"Failed to fetch diff: {resp.status_code}")
            return
        diff_payload = resp.text

    print(f"🔵 [PRAGMA LOG] Initializing LangGraph checkpointer thread: {run_id}...")
    # 2. Compile Graph and map to Postgres checkpoint saver
    graph = await compile_pragma_graph(pool)
    config = {"configurable": {"thread_id": run_id}}
    
    initial_state = {
        "thread_id": run_id,
        "pr_number": pr_number,
        "repository": full_name,
        "diff_payload": diff_payload
    }
    
    try:
        print("🟡 [PRAGMA LOG] Invoking LangGraph agent workflow...")
        # 3. Trigger LangGraph asynchronous workflow
        await graph.ainvoke(initial_state, config=config)
        
        print("🟠 [PRAGMA LOG] Agent workflow suspended/completed. Issuing outbound GitHub API comment...")
        # 4. Post Live UI Verification Link Back to GitHub Issues API
        comment_body = f"""### 🤖 PRAGMA // Autonomous Code Review Intercepted

Potential architectural execution anomalies or security risks were caught in this commit frame. 

🔍 **[Click here to view the deep analysis and approve this build](https://pragma.zainiqbal.tech/?run_id={run_id})**"""
        
        comments_url = f"https://api.github.com/repos/{full_name}/issues/{pr_number}/comments"
        
        async with httpx.AsyncClient() as client:
            await client.post(
                comments_url,
                headers={
                    "Authorization": f"token {github_pat}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={"body": comment_body}
            )
        
        print(f"✅ [PRAGMA LOG] GitHub comment posted successfully for run {run_id}.")
            
    except Exception as e:
        print(f"Error in background webhook processing: {e}")


@app.post("/api/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handles incoming GitHub pull_request events.
    """
    event = request.headers.get("X-GitHub-Event")
    if event != "pull_request":
        return {"status": "ignored", "reason": f"Event '{event}' is not pull_request"}
        
    payload = await request.json()
    action = payload.get("action")
    if action not in ["opened", "synchronize"]:
        return {"status": "ignored", "reason": f"Action '{action}' ignored"}
        
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    
    diff_url = pr.get("diff_url")
    full_name = repo.get("full_name")
    pr_number = pr.get("number")
    head_sha = pr.get("head", {}).get("sha")
    
    if not all([diff_url, full_name, pr_number, head_sha]):
        raise HTTPException(status_code=400, detail="Missing required PR payload fields")
        
    run_id = str(uuid.uuid4())
    pool = request.app.state.db_pool
    background_tasks.add_task(process_github_webhook, run_id, pr_number, full_name, diff_url, head_sha, pool)
    
    return {"status": "accepted", "run_id": run_id}

@app.get("/api/status")
async def get_status():
    """Health check endpoint for frontend."""
    return {"status": "ok", "message": "API is online"}

@app.patch("/api/reviews/{run_id}/approve")
async def resume_run(run_id: str, request: Request):
    """
    Resumes the LangGraph execution from the HITL (Human In The Loop) interrupt checkpoint.
    """
    pool = request.app.state.db_pool
        
    # Re-initialize the LangGraph instance mapping to our Postgres saver
    graph = await compile_pragma_graph(pool)
    
    # Map configuration to the exact run_id thread
    config = {
        "configurable": {
            "thread_id": run_id
        }
    }
    
    try:
        # Passing None forces LangGraph to resume cleanly from where it paused
        await graph.ainvoke(None, config=config)
        return {"status": "success", "message": f"Run {run_id} successfully resumed."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)