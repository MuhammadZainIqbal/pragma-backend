import os
import httpx
from typing import Tuple
from unidiff import PatchSet

from langgraph.graph import StateGraph, END
from langgraph.constants import Send
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.graphs.state import PRReviewState, FileChunk
from app.nodes.critic import critic_node
from app.nodes.security_agent import security_agent_node
from app.nodes.architecture_agent import architecture_agent_node
from app.nodes.style_agent import style_agent_node
from app.nodes.publisher import publisher_node


async def pr_fetcher(state: PRReviewState):
    """Ingests diff and chunks it into state.file_chunks from GitHub API."""
    github_pat = os.environ.get("GITHUB_PAT")
    if not github_pat:
        raise ValueError("GITHUB_PAT environment variable is missing.")

    headers = {
        "Authorization": f"token {github_pat}",
        "Accept": "application/vnd.github.v3.diff",
    }
    url = f"https://api.github.com/repos/{state.repository}/pulls/{state.pr_number}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        
        # Raise an explicit error if the API rejects the request or fails to find it
        if response.status_code != 200:
            raise ValueError(f"GitHub API Error {response.status_code}: {response.text}")

        diff_content = response.text
        print(f"[DEBUG GITHUB DIFF] Raw Diff Content: '{diff_content}'")
        
        if not diff_content.strip():
            raise ValueError("GitHub API returned an empty diff string.")

        # Chunk the raw patch into individual files using unidiff
        try:
            patch = PatchSet(diff_content.splitlines(keepends=True))
        except Exception as e:
            raise ValueError(f"Failed to parse unified diff format: {e}")

        file_chunks = []
        for patched_file in patch:
            if patched_file.is_binary_file:
                continue
            file_path = patched_file.target_file.lstrip('b/').lstrip('/')
            hunks = [str(h) for h in patched_file]
            file_chunks.append(FileChunk(file_path=file_path, hunks=hunks))

        return {
            "diff_payload": diff_content,
            "file_chunks": file_chunks
        }


async def hitl_gate(state: PRReviewState):
    """Human-in-the-loop serialization gate. Graph interrupts here before publisher."""
    return {}


# --- Routing Logic ---

def fan_out_router(state: PRReviewState):
    """
    Dispatches file chunks concurrently to all three agent nodes 
    using LangGraph's Send() API.
    """
    sends = []
    for chunk in state.file_chunks:
        # Pushes isolated chunk state to each agent
        sends.append(Send("security_agent_node", {"file_chunks": [chunk]}))
        sends.append(Send("architecture_agent_node", {"file_chunks": [chunk]}))
        sends.append(Send("style_agent_node", {"file_chunks": [chunk]}))
    
    # If no chunks, bypass agents directly to critic
    return sends if sends else "critic"

def critic_router(state: PRReviewState):
    """
    Conditional edge router out of the critic node.
    If quality fails and we have retries, rebuild the Send() objects to re-run the agents.
    """
    if state.pr_quality_score >= 0.7 or state.critic_retry_count >= 2:
        return "hitl_gate"
    else:
        # Rerun graph fan-out mapping via the initial router
        return fan_out_router(state)


# --- Graph Assembly ---

async def compile_pragma_graph(pool: AsyncConnectionPool) -> object:
    """
    Assembles the LangGraph DAG, binds the Supabase AsyncPostgresSaver,
    and applies HITL interruption before the publisher node.
    Returns the compiled graph.
    """
    builder = StateGraph(PRReviewState)

    # 1. Register Nodes
    builder.add_node("pr_fetcher", pr_fetcher)
    builder.add_node("security_agent_node", security_agent_node)
    builder.add_node("architecture_agent_node", architecture_agent_node)
    builder.add_node("style_agent_node", style_agent_node)
    builder.add_node("critic", critic_node)
    builder.add_node("hitl_gate", hitl_gate)
    builder.add_node("publisher", publisher_node)

    # 2. Define Entry Point
    builder.set_entry_point("pr_fetcher")

    # 3. Dynamic Parallel Fan-Out via Send() API
    builder.add_conditional_edges(
        "pr_fetcher",
        fan_out_router,
        ["security_agent_node", "architecture_agent_node", "style_agent_node", "critic"]
    )

    # 4. Fan-In — all agent branches collapse into the Critic Node
    builder.add_edge("security_agent_node", "critic")
    builder.add_edge("architecture_agent_node", "critic")
    builder.add_edge("style_agent_node", "critic")

    # 5. Conditional Routing & Feedback Loop
    builder.add_conditional_edges(
        "critic",
        critic_router,
        ["hitl_gate", "security_agent_node", "architecture_agent_node", "style_agent_node", "critic"]
    )

    # 6. HITL Gate → Publisher → END
    builder.add_edge("hitl_gate", "publisher")
    builder.add_edge("publisher", END)

    # 7. Bind Supabase PostgreSQL State Checkpointer
    checkpointer = AsyncPostgresSaver(pool)

    # Auto-initialize checkpointer schema tables on first cold boot
    await checkpointer.setup()

    # 8. Compile with HITL interrupt gate
    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["publisher"]
    )

    # Return graph only
    return graph
