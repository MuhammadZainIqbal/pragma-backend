import argparse
import asyncio
import json
import os
import sys
import traceback
from typing import Optional
from dotenv import load_dotenv

# Execute immediately before any application logic
load_dotenv()
load_dotenv("frontend/.env")

from app.graphs.graph import compile_pragma_graph
from app.graphs.state import AgentFinding, PRReviewState


def _fail_safe_log(run_id: str, error: str) -> None:
    """Best-effort Supabase status update on catastrophic crash. Fire-and-forget sync."""
    try:
        from supabase import create_client

        supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY", "")

        if supabase_url and supabase_key:
            supabase = create_client(supabase_url, supabase_key)
            supabase.table("github_webhook_payloads").update(
                {"status": "failed"}
            ).eq("run_id", run_id).execute()
            print("Successfully logged 'failed' status to Supabase payload history.")
        else:
            print("Warning: Supabase credentials missing; cannot log failure state.", file=sys.stderr)
    except Exception as db_err:
        print(f"FATAL: DB error while logging crash: {db_err}", file=sys.stderr)


async def main():
    parser = argparse.ArgumentParser(description="PRAGMA LangGraph Orchestrator Worker CLI")
    parser.add_argument("--run-id", required=True, help="Unique Run ID (UUIDv4)")
    parser.add_argument("--pr", required=True, help="Pull Request Number")
    parser.add_argument("--repo", required=True, help="Repository Full Name (owner/repo)")
    parser.add_argument(
        "--mode",
        choices=["initial", "resume"],
        default="initial",
        help="Execution mode: 'initial' for first run, 'resume' to apply HITL edits and publish."
    )
    # Resume mode supplies human-edited findings as a JSON string
    parser.add_argument(
        "--edits",
        default=None,
        help="JSON-stringified array of human-edited AgentFinding objects (resume mode only)."
    )
    args = parser.parse_args()

    run_id: str = args.run_id
    pr_number: str = args.pr
    repo_name: str = args.repo
    mode: str = args.mode
    raw_edits: Optional[str] = args.edits

    print(f"[Worker Start] PRAGMA | mode={mode} | repo={repo_name} | PR=#{pr_number} | run_id={run_id}")

    # 1. Retrieve essential DB connection string from environment
    # Gracefully fall back to DB_CONNECTION_STRING if DATABASE_URL is not set
    db_conn = os.environ.get("DATABASE_URL") or os.environ.get("DB_CONNECTION_STRING")
    if not db_conn:
        print("ERROR: DATABASE_URL or DB_CONNECTION_STRING environment variable is missing.", file=sys.stderr)
        sys.exit(1)

    # 2. Compile LangGraph with AsyncPostgresSaver bindings
    # graph.py now exposes the pool so we own the connection lifecycle
    try:
        app, pool = await compile_pragma_graph(db_conn)
    except Exception as e:
        print(f"ERROR: Failed to compile the LangGraph DAG: {e}", file=sys.stderr)
        sys.exit(1)

    config = {"configurable": {"thread_id": run_id}}

    # 3. Wrap the entire invocation in try/finally to guarantee pool closure
    try:
        if mode == "initial":
            # --- INITIAL MODE ---
            # Build pristine PRReviewState and kick off full graph execution
            initial_state = PRReviewState(
                thread_id=run_id,
                pr_number=int(pr_number),
                repository=repo_name,
            )
            await app.ainvoke(initial_state, config=config)
            print(f"[Worker Success] Initial graph execution completed for Run ID: {run_id}")

        elif mode == "resume":
            # --- RESUME MODE ---
            # Parse human-edited findings from the CLI argument
            if not raw_edits:
                print("ERROR: --edits argument is required in resume mode.", file=sys.stderr)
                sys.exit(1)

            try:
                edits_payload = json.loads(raw_edits)
                human_findings = [AgentFinding(**f) for f in edits_payload]
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                print(f"ERROR: Failed to parse --edits JSON payload: {e}", file=sys.stderr)
                sys.exit(1)

            # Apply HITL modifications directly to the Supabase checkpointer state
            # This replaces consolidated_findings with the human-vetted list before resuming
            app.update_state(config, {"final_findings": human_findings})
            print(f"[Worker Resume] Applied {len(human_findings)} HITL finding(s) to checkpointer state.")

            # Resume graph from the interrupt point (publisher node)
            # Passing None as input instructs LangGraph to continue from last checkpoint
            await app.ainvoke(None, config=config)
            print(f"[Worker Success] Resumed graph execution completed for Run ID: {run_id}")

        # --- DB SYNC (Because CLI execution bypasses the webhook ingestion) ---
        try:
            from supabase import create_client
            supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY", "")

            if supabase_url and supabase_key:
                supabase_client = create_client(supabase_url, supabase_key)
                final_state = await app.aget_state(config)
                
                if final_state and final_state.values:
                    state_values = final_state.values
                    
                    # 1. Update / Insert the webhook payload record
                    raw_findings = state_values.get("final_findings", [])
                    final_findings_json = [f.model_dump() for f in raw_findings] if raw_findings else []
                    
                    # Compute status based on mode and state
                    # If initial hit the interrupt, it's paused. If resumed it's complete.
                    current_status = "paused_hitl" if mode == "initial" else "complete"

                    payload_data = {
                        "run_id": run_id,
                        "repository": repo_name,
                        "pr_number": int(pr_number),
                        "status": current_status,
                        "final_findings": final_findings_json,
                        "pr_quality_score": state_values.get("pr_quality_score", 1.0)
                    }
                    supabase_client.table("github_webhook_payloads").upsert(payload_data).execute()

                    # 2. Insert telemetry ticks 
                    raw_telemetry = state_values.get("telemetry", [])
                    if raw_telemetry:
                        telemetry_ticks = [
                            {
                                "run_id": run_id,
                                "node_name": t.node_name,
                                "execution_time_ms": t.execution_time_ms,
                                "input_tokens": t.input_tokens,
                                "output_tokens": t.output_tokens,
                                "cost_usd": t.cost_usd,
                            }
                            for t in raw_telemetry
                        ]
                        
                        # Use upsert to avoid duplicates if re-running or we can just insert
                        # Telemetry doesn't have a unique constraint on run_id + node_name usually, but we will insert
                        supabase_client.table("review_telemetry").insert(telemetry_ticks).execute()
                        
                    print("[Worker DB Sync] Successfully pushed state to Supabase custom tables.")
        except Exception as sync_err:
            print(f"Warning: Failed to sync state to Supabase custom tables: {sync_err}", file=sys.stderr)

    except Exception as e:
        print(f"ERROR: Graph execution crashed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _fail_safe_log(run_id, str(e))
        sys.exit(1)

    finally:
        # Always explicitly close the connection pool to prevent orphaned connections
        # on the Supabase free tier which aggressively reaps idle connections
        try:
            await pool.close()
            print("[Worker Cleanup] AsyncPostgresSaver connection pool closed cleanly.")
        except Exception as pool_err:
            print(f"Warning: Error while closing connection pool: {pool_err}", file=sys.stderr)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
