import time
import asyncio
from typing import List
from pydantic import BaseModel

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

    try:
        parsed, usage = await KeyManager.execute_with_key_rotation(
            system_prompt=system_prompt,
            prompt_text=prompt_content,
            response_schema=SecurityFindingsList
        )
        findings = parsed.findings
        
        if usage:
            input_tokens = getattr(usage, "prompt_token_count", 0)
            output_tokens = getattr(usage, "candidates_token_count", 0)
            
    except Exception as e:
        print(f"Node security_agent_node failed completely: {e}")

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
