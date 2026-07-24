# PRAGMA // Backend Gateway & Orchestration Engine

The backend API and orchestration engine for PRAGMA. Built with Python, FastAPI, and LangGraph, this service processes incoming GitHub webhook events, coordinates parallel multi-agent evaluation workflows using Gemini, manages API key rotation and concurrency locks, and persists thread state to PostgreSQL via Supabase.

---

## Technical Overview

- **Framework:** FastAPI 0.111 / Uvicorn
- **Orchestration:** LangGraph 0.2.60 (Async State Graph with `Send()` parallel fan-out)
- **Model Engine:** Google GenAI SDK (`gemini-3.5-flash`) with structured Pydantic v2 schemas
- **State Persistence:** `AsyncPostgresSaver` via `langgraph-checkpoint-postgres` (Supabase PostgreSQL)
- **Diff Parsing:** `unidiff` for deterministic unified diff verification
- **Integrations:** GitHub REST API (Webhooks, Pull Request Comments, Repository Dispatch)
- **Deployment:** Render Web Service

---

## System Architecture

### 1. Webhook Ingestion

Two ingestion paths exist depending on the deployment model:

- **Direct Processing (`app/main.py`):** Receives `pull_request` events on `POST /api/webhook`, fetches the raw unified diff from GitHub, compiles the LangGraph DAG, and invokes the full workflow as a background task. Posts a review link comment back to the PR upon completion.
- **Dispatch Model (`app/api/webhook.py`):** A serverless-safe ingestion endpoint on `POST /webhook/github` that validates HMAC SHA-256 signatures (`X-Hub-Signature-256`), logs raw payloads to Supabase, and fires a `repository_dispatch` event to trigger the CLI worker via GitHub Actions.

### 2. LangGraph DAG Topology

The graph is assembled in `app/graphs/graph.py` using `StateGraph(PRReviewState)`:

```
pr_fetcher
    |
    v
fan_out_router (conditional Send() dispatch)
    |
    +---> security_agent_node
    +---> architecture_agent_node
    +---> style_agent_node
    |         |                |
    +---------+--------+-------+
                       |
                       v
                    critic
                       |
                  critic_router
                  /          \
          (score < 0.7        (score >= 0.7
           AND retries < 2)    OR retries >= 2)
              |                     |
         [re-fan-out]          hitl_gate
                               (interrupt_before publisher)
                                    |
                                    v
                                publisher --> END
```

- **`pr_fetcher`:** Fetches the raw diff from the GitHub REST API and chunks it into `FileChunk` objects using `unidiff.PatchSet`.
- **`fan_out_router`:** Uses LangGraph's `Send()` API to dispatch each file chunk concurrently to all three agent nodes.
- **`critic`:** Verifies agent `diff_citation` fields against actual added lines parsed from the unified diff. Deduplicates findings by `(file_path, line_number)`, elevates severity on conflict, and scores overall PR quality via a structured Gemini call.
- **`critic_router`:** If `pr_quality_score < 0.7` and `critic_retry_count < 2`, re-triggers the full fan-out. Otherwise, routes to the HITL gate.
- **`hitl_gate`:** A no-op node placed before `publisher`. The graph is compiled with `interrupt_before=["publisher"]`, suspending execution here until a human approves via the dashboard.
- **`publisher`:** Formats consolidated findings and posts a review comment to the GitHub PR.

### 3. Multi-Agent Graph Nodes

| Node | File | Responsibility |
| :--- | :--- | :--- |
| Security Agent | `app/nodes/security_agent.py` | SQL injection, command execution, prototype pollution, secret exposure, memory-unsafe patterns |
| Architecture Agent | `app/nodes/architecture_agent.py` | Async concurrency issues, connection leaks, N+1 queries, circular dependencies, API anti-patterns |
| Style Agent | `app/nodes/style_agent.py` | Naming conventions, type safety, dead code, off-by-one errors, readability |
| Critic | `app/nodes/critic.py` | Diff-verified deduplication, hallucination guard, quality scoring (0.0-1.0) |
| Publisher | `app/nodes/publisher.py` | Formats and posts final review comment to GitHub PR |

### 4. Key Manager & Concurrency Control

Defined in `app/utils/key_manager.py`:

- Enforces a global `asyncio.Semaphore(1)` lock so exactly one LLM request hits the Gemini API at any given time, preventing 503 rate-limit bursts during parallel fan-out.
- Implements round-robin key rotation across `GEMINI_API_KEYS` with exponential backoff retries (up to 5 attempts).
- Returns both the parsed Pydantic response and raw `usage_metadata` for telemetry tracking.

### 5. State Schema

The core typed state is `PRReviewState` (defined in `app/graphs/state.py`), a strict Pydantic v2 model. Key design decisions:

- **Parallel merge via `Annotated[List, operator.add]`:** The `security_findings`, `architecture_findings`, `style_findings`, and `telemetry` fields use the `operator.add` reducer, allowing LangGraph to merge results from concurrent fan-out branches without overwriting.
- **Computed fields:** `total_cost_usd` and `critical_finding_count` are `@computed_field` properties derived at read time.

### 6. CLI Worker

`app/cli/run_graph.py` provides a standalone CLI entrypoint for GitHub Actions workflows:

```bash
python -m app.cli.run_graph \
  --run-id <uuid> \
  --pr <number> \
  --repo <owner/repo> \
  --mode initial|resume \
  --edits '<json>'  # resume mode only
```

In `initial` mode, it compiles the graph and invokes a fresh `PRReviewState`. In `resume` mode, it deserializes human-edited findings from the `--edits` JSON argument, applies them to the checkpointer state, and resumes execution from the HITL interrupt point. After execution, it syncs final state (findings, telemetry, quality score) to Supabase custom tables.

---

## API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/webhook` | Receives GitHub `pull_request` events and spawns background graph execution. |
| `POST` | `/webhook/github` | HMAC-verified serverless ingestion; logs to Supabase and dispatches GitHub Actions worker. |
| `GET` | `/api/state` | Retrieves LangGraph thread state and review findings for a given `run_id`. |
| `PATCH` | `/api/reviews/{run_id}/approve` | Resumes the LangGraph execution from the HITL interrupt checkpoint. |
| `GET` | `/api/reviews/{run_id}` | Returns stored findings and metadata for a run from Supabase. |
| `GET` | `/api/test-keys` | Diagnostic endpoint validating the operational health of configured Gemini API keys. |
| `GET` | `/api/status` | Liveness check returning service status. |

---

## Environment Variables

Create a `.env` file in the project root:

```env
# Gemini Configuration
GEMINI_API_KEYS=key1,key2,key3,key4

# GitHub Authentication
GITHUB_PAT=ghp_your_personal_access_token
GITHUB_WEBHOOK_SECRET=your_webhook_hmac_secret
GITHUB_OWNER=MuhammadZainIqbal
GITHUB_REPO=pragma-backend

# Database (Supabase PostgreSQL)
DATABASE_URL=postgresql://user:password@host:5432/dbname

# Supabase Client (for direct table operations)
SUPABASE_URL=https://<project-id>.supabase.co
SUPABASE_KEY=your_supabase_service_role_or_anon_key

# Runtime
ENVIRONMENT=development
PORT=10000
```

---

## Local Development

1. **Clone repository:**
```bash
git clone https://github.com/MuhammadZainIqbal/pragma-backend.git
cd pragma-backend
```

2. **Set up virtual environment:**
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

4. **Run development server:**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

5. **Run API key diagnostics (via live endpoint):**
```bash
curl http://localhost:8000/api/test-keys
```

6. **Run CLI worker directly (bypasses webhook):**
```bash
python -m app.cli.run_graph --run-id <uuid> --pr <number> --repo <owner/repo> --mode initial
```

---

## Project Structure

```text
pragma-backend/
├── app/
│   ├── main.py                         # FastAPI entry point, lifespan, direct webhook & state routes
│   ├── api/
│   │   ├── webhook.py                  # HMAC-verified serverless webhook ingestion & dispatch
│   │   └── reviews.py                  # Review CRUD & HITL approve/resume endpoint
│   ├── graphs/
│   │   ├── graph.py                    # LangGraph DAG assembly, fan-out routing, checkpointer binding
│   │   └── state.py                    # PRReviewState, AgentFinding, NodeTelemetry Pydantic schemas
│   ├── nodes/
│   │   ├── security_agent.py           # Security evaluation node
│   │   ├── architecture_agent.py       # Architectural analysis node
│   │   ├── style_agent.py              # Style & quality evaluation node
│   │   ├── critic.py                   # Consensus, deduplication & quality scoring node
│   │   └── publisher.py                # GitHub PR comment publisher node
│   ├── utils/
│   │   └── key_manager.py              # Gemini API key rotation, semaphore locking, retry logic
│   └── cli/
│       └── run_graph.py                # Standalone CLI worker for GitHub Actions execution
├── requirements.txt
└── .gitignore
```

---

## Dependencies

| Package | Purpose |
| :--- | :--- |
| `fastapi` / `uvicorn` | HTTP framework and ASGI server |
| `langgraph` | Async state graph orchestration with conditional routing |
| `langgraph-checkpoint-postgres` | PostgreSQL-backed state persistence for HITL interrupts |
| `google-genai` | Native Gemini SDK for structured LLM output generation |
| `pydantic` | Strict schema validation for state, findings, and LLM responses |
| `unidiff` | Deterministic unified diff parsing for citation verification |
| `psycopg` / `psycopg-pool` | Async PostgreSQL connection pooling |
| `httpx` | Async HTTP client for GitHub API and webhook dispatch |
| `supabase` | Direct Supabase table operations for payload logging |
| `python-dotenv` | Environment variable loading from `.env` files |

---

## Data Flow Summary

1. **Ingestion:** GitHub fires a `pull_request` webhook. The server validates the event, generates a `run_id` (UUIDv4), and spawns background processing.
2. **Diff Fetching:** `pr_fetcher` retrieves the raw unified diff from GitHub and chunks it into `FileChunk` objects via `unidiff.PatchSet`.
3. **Parallel Analysis:** `fan_out_router` dispatches each chunk concurrently to Security, Architecture, and Style agent nodes using LangGraph `Send()`.
4. **Consensus:** The Critic node verifies all agent citations against actual diff lines, deduplicates by file and line number, and scores overall PR quality.
5. **Feedback Loop:** If quality is below threshold and retries remain, the graph re-invokes the agent fan-out for a second pass.
6. **HITL Gate:** The graph suspends before the Publisher node. A comment on the GitHub PR links to the frontend dashboard where a human reviews findings.
7. **Resume:** Upon dashboard approval, the graph resumes from the checkpoint and the Publisher posts the final review comment to GitHub.