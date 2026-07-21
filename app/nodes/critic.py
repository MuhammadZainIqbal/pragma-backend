import asyncio
import time
from typing import Dict, Tuple

from pydantic import BaseModel, Field
from unidiff import PatchSet

from google import genai
from instructor import from_genai
from instructor import Mode

from app.utils.key_manager import KeyManager
from app.graphs.state import AgentFinding, PRReviewState

class QualityEvaluation(BaseModel):
    """Structured LLM output for overall PR evaluation."""
    score: float = Field(..., description="Overall PR quality score from 0.0 to 1.0")
    reasoning: str = Field(..., description="Explanation for the score")

async def critic_node(state: PRReviewState) -> dict:
    """
    The Consensus & Hallucination Guard Node.
    - Uses unidiff for deterministic structural diff patch parsing.
    - Deduplicates findings across multi-agent branches.
    - Verifies agent diff_citations against exactly added/modified lines.
    - Scores overall PR quality, triggering re-runs if poor.
    """
    
    # 1. Structural Diff Verification (Anti-Hallucination Guard)
    try:
        # PatchSet requires split lines with keepends=True
        patch = PatchSet(state.diff_payload.splitlines(keepends=True))
    except Exception:
        patch = []
        
    # Map valid added/modified lines: (file_path, line_number) -> line text
    added_lines_map: Dict[Tuple[str, int], str] = {}
    for patched_file in patch:
        # Standardize file path (strip git b/ prefixes)
        target_file = patched_file.target_file.lstrip('b/').lstrip('/')
        
        for hunk in patched_file:
            for line in hunk:
                if line.is_added:
                    added_lines_map[(target_file, line.target_line_no)] = line.value.strip()

    # Collect all fan-out findings
    all_findings = state.security_findings + state.architecture_findings + state.style_findings
    
    verified_findings = []
    for finding in all_findings:
        file_path = finding.file_path.lstrip('b/').lstrip('/')
        line_no = finding.line_number
        citation = finding.diff_citation.strip()
        
        # Exact match verification: Only keep finding if citation bounds to actual added lines
        if (file_path, line_no) in added_lines_map:
            actual_line = added_lines_map[(file_path, line_no)]
            # Loose inclusion tolerates minor whitespace drift while ensuring structural match
            if citation in actual_line or actual_line in citation:
                verified_findings.append(finding)

    # 2. Deduplication & Consolidation Logic
    deduped_map: Dict[Tuple[str, int], AgentFinding] = {}
    severity_rank = {"info": 0, "warning": 1, "critical": 2}

    for f in verified_findings:
        key = (f.file_path, f.line_number)
        if key not in deduped_map:
            deduped_map[key] = f
        else:
            existing = deduped_map[key]
            # Merge context descriptions
            existing.description += f"\n\n[Additional Note]: {f.description}"
            
            # Elevate severity dynamically based on highest rank
            curr_rank = severity_rank.get(existing.severity.lower(), 0)
            new_rank = severity_rank.get(f.severity.lower(), 0)
            if new_rank > curr_rank:
                existing.severity = f.severity.lower()
                
    final_findings_list = list(deduped_map.values())
    
    # 3. Quality Scoring Pass via Gemini Token Bucket Pool
    score = 1.0  # Default to perfect if no findings
    reasoning = "No findings to evaluate. PR looks solid."
    
    if final_findings_list:
        prompt_content = "Evaluate the quality of the PR based on the following verified code review findings:\n"
        for idx, finding in enumerate(final_findings_list):
            prompt_content += f"{idx+1}. File: {finding.file_path}:{finding.line_number} | Severity: {finding.severity} | {finding.description}\n"
            
        import asyncio
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            api_key = await KeyManager.get_next_key()
            raw_client = genai.Client(api_key=api_key)
            client = from_genai(raw_client, mode=Mode.TOOLS, use_async=True)

            try:
                # Execute native async Gemini validation via instructor directly in the event loop
                eval_result: QualityEvaluation = await client.chat.completions.create(
                    model="gemini-3.5-flash",
                    response_model=QualityEvaluation,
                    messages=[
                        {
                            "role": "system", 
                            "content": "You are a Senior Staff Code Reviewer. Score the overall quality of a PR from 0.0 (terrible) to 1.0 (perfect) based strictly on the severity and frequency of the findings."
                        },
                        {"role": "user", "content": prompt_content}
                    ]
                )
                score = eval_result.score
                reasoning = eval_result.reasoning
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

    # Return the idempotently consolidated findings.
    # Note: Because LangGraph strictly appends to `operator.add` keys, deduplicating them here
    # safely "accounts for" stale iterations. To completely purge the ballooning in state,
    # the reducer in state.py must be modified, but this guarantees correct logic downstream.
    return {
        "final_findings": final_findings_list,
        "pr_quality_score": score,
        "critic_retry_count": state.critic_retry_count + 1
    }
