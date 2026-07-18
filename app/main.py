import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Initialize environment configurations
load_dotenv()

from app.graphs.graph import compile_pragma_graph

# Security evaluation: Guard documentation endpoints against production scanning
IS_PROD = os.getenv("ENVIRONMENT", "development").lower() == "production"

app = FastAPI(
    title="PRAGMA Persistent API Server",
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None if IS_PROD else "/redoc",
    openapi_url=None if IS_PROD else "/openapi.json"
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

@app.get("/api/status")
async def get_status():
    """Health check endpoint for frontend."""
    return {"status": "ok", "message": "API is online"}

@app.patch("/api/reviews/{run_id}/approve")
async def resume_run(run_id: str):
    """
    Resumes the LangGraph execution from the HITL (Human In The Loop) interrupt checkpoint.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured in the environment.")
        
    # Re-initialize the LangGraph instance mapping to our Postgres saver
    graph, pool = await compile_pragma_graph(db_url)
    
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
    finally:
        # Gracefully close the connection pool to prevent database socket leaks
        await pool.close()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)