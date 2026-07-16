import httpx
import os
import time

from app.graphs.state import PRReviewState, NodeTelemetry

async def publisher_node(state: PRReviewState) -> dict:
    """
    Final Delivery Node.
    Compiles vetted findings into the strict GitHub v3 REST API format,
    handling single vs multi-line comment constraints, and dispatches the review payload.
    """
    start_time = time.time()
    
    findings = state.final_findings
    if not findings:
        return {}

    github_pat = os.environ.get("GITHUB_PAT")
    if not github_pat:
        print("CRITICAL: Missing GITHUB_PAT. Cannot publish review to GitHub.")
        return {}

    repo = state.repository
    pr_number = state.pr_number
    
    if not repo or not pr_number:
        print("CRITICAL: Repository or PR Number not found in state context. Cannot publish.")
        return {}

    comments = []
    critical_count = 0

    for finding in findings:
        if finding.severity.lower() == "critical":
            critical_count += 1
            
        body = f"**[{finding.severity.upper()}]**\n{finding.description}\n\n**Suggestion:**\n{finding.suggestion}"
        
        line = finding.line_number
        start_line = getattr(finding, "start_line", None)
        
        # GitHub API Multi-line Comment Validation (Catastrophic 422 Error Guard)
        # 1. If line_start == line_end, omit start_line.
        # 2. If line_start < line_end, require line, start_line, and side: RIGHT.
        if start_line is not None and start_line < line:
            comment = {
                "path": finding.file_path,
                "body": body,
                "line": line,
                "start_line": start_line,
                "side": "RIGHT"
            }
        else:
            comment = {
                "path": finding.file_path,
                "body": body,
                "line": line,
                "side": "RIGHT"
            }
        comments.append(comment)

    # Set final event status
    event = "REQUEST_CHANGES" if critical_count > 0 else "COMMENT"
    
    payload = {
        "body": f"### PRAGMA Autonomous Review Complete\n- **Quality Score:** {state.pr_quality_score:.2f}/1.0\n- **Findings Addressed:** {len(findings)}\n\n_Generated securely by LangGraph Compute Engine._",
        "event": event,
        "comments": comments
    }

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {github_pat}",
    }

    # Dispatch to GitHub
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
            resp.raise_for_status()
            print(f"Successfully published PRAGMA review to {repo} PR #{pr_number}.")
        except Exception as e:
            print(f"Failed to publish review to GitHub API: {e}")
            if isinstance(e, httpx.HTTPStatusError):
                print(f"Response Body: {e.response.text}")

    # Telemetry logging for the Publisher Node itself
    exec_time_ms = (time.time() - start_time) * 1000
    telemetry = NodeTelemetry(
        node_name="publisher_node",
        execution_time_ms=exec_time_ms,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0
    )

    return {"telemetry": [telemetry]}
