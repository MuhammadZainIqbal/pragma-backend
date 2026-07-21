import time
from typing import List
from pydantic import BaseModel

from google import genai
from instructor.v2 import from_genai
from instructor import Mode

from app.utils.key_manager import KeyManager
from app.graphs.state import PRReviewState, AgentFinding, NodeTelemetry

class SecurityFindingsList(BaseModel):
    findings: List[AgentFinding]

# Gemini 2.0 Flash Approximate Pricing
GEMINI_INPUT_COST_1M = 0.10
GEMINI_OUTPUT_COST_1M = 0.40

async def security_agent_node(state) -> dict:
    start_time = time.time()
    chunks = getattr(state, "file_chunks", state.get("file_chunks", [])) if isinstance(state, dict) else state.file_chunks
    
    if not chunks:
        return {}

    prompt_content = "Analyze the following code patches for Security vulnerabilities.\n\n"
    for chunk in chunks:
        prompt_content += f"File: {chunk.file_path}\n"
        for hunk in chunk.hunks:
            prompt_content += f"{hunk}\n"
            
    system_prompt = (
        "You are an elite Application Security Engineer. Review the provided code diffs specifically for security flaws. "
        "Focus on OWASP Top 10 risks: injection, hardcoded secrets, broken authentication, SSRF, and exposed sensitive data. "
        "Return a structured list of AgentFindings. For multi-line vulnerabilities, populate both start_line and line_number (end line). "
        "Set diff_citation to the exact vulnerable code snippet from the diff. If no vulnerabilities exist, return an empty list."
    )

    input_tokens = 0
    output_tokens = 0
    findings = []

    import asyncio
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        api_key = await KeyManager.get_next_key()
        raw_client = genai.Client(api_key=api_key)
        client = from_genai(raw_client, mode=Mode.TOOLS, use_async=True)

        try:
            parsed, raw = await client.chat.completions.create_with_completion(
                model="gemini-3.5-flash",
                response_model=SecurityFindingsList,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt_content}
                ]
            )
            findings = parsed.findings
            
            if hasattr(raw, "usage_metadata") and raw.usage_metadata:
                input_tokens = getattr(raw.usage_metadata, "prompt_token_count", 0)
                output_tokens = getattr(raw.usage_metadata, "candidates_token_count", 0)
            break
            
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "rate limit" in error_str or "exhausted" in error_str:
                await KeyManager.report_rate_limit(api_key)
            elif "503" in error_str or "unavailable" in error_str:
                print(f"[WARNING] 503 UNAVAILABLE on key ...{api_key[-6:]}: Google's API is heavily loaded.")
                
            if attempt == max_attempts:
                raise e
                
            import random
            backoff_delay = (2 ** attempt) + random.uniform(1.0, 3.0)
            print(f"⚠️ [PRAGMA KEY MANAGER] Key ...{api_key[-6:]} hit 503 spike. Retrying in {backoff_delay:.2f}s (Attempt {attempt}/5)...")
            await asyncio.sleep(backoff_delay)

    exec_time_ms = (time.time() - start_time) * 1000
    cost = (input_tokens / 1_000_000 * GEMINI_INPUT_COST_1M) + (output_tokens / 1_000_000 * GEMINI_OUTPUT_COST_1M)

    telemetry = NodeTelemetry(
        node_name="security_agent_node",
        execution_time_ms=exec_time_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost
    )

    return {
        "security_findings": findings,
        "telemetry": [telemetry]
    }
