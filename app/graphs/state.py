import operator
from typing import Annotated, List, Optional

from pydantic import BaseModel, Field, computed_field


class FileChunk(BaseModel):
    """Represents a chunk or hunk of a file parsed from a unified diff."""
    file_path: str
    hunks: List[str]


class AgentFinding(BaseModel):
    """A specific issue or finding discovered by an agent during the review."""
    file_path: str
    line_number: int = Field(..., description="The exact end line number of the code finding.")
    start_line: Optional[int] = Field(default=None, description="The start line number for multi-line findings. If single line, leave None.")
    diff_citation: str = Field(
        ...,
        description="The exact snippet from the diff the finding relates to. Used by Critic for verification."
    )
    severity: str = Field(
        ...,
        description="Severity of the finding. E.g., 'critical', 'warning', 'info'."
    )
    description: str
    suggestion: str


class NodeTelemetry(BaseModel):
    """Tracks execution time and token usage for a specific node/agent."""
    node_name: str
    execution_time_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class PRReviewState(BaseModel):
    """
    Core state for the LangGraph PR Review workflow.
    Uses strict Pydantic v2 data compilation.
    """
    thread_id: str
    pr_number: int = Field(default=0)
    repository: str = Field(default="")
    diff_payload: str = Field(default="")
    file_chunks: List[FileChunk] = Field(default_factory=list)

    # Parallel fan-out keys utilizing operator.add to prevent context overwrites
    security_findings: Annotated[List[AgentFinding], operator.add] = Field(default_factory=list)
    architecture_findings: Annotated[List[AgentFinding], operator.add] = Field(default_factory=list)
    style_findings: Annotated[List[AgentFinding], operator.add] = Field(default_factory=list)

    # Post-consensus state
    final_findings: List[AgentFinding] = Field(default_factory=list)
    critic_retry_count: int = Field(default=0)
    pr_quality_score: float = Field(default=0.0)

    # Telemetry and observability
    telemetry: Annotated[List[NodeTelemetry], operator.add] = Field(default_factory=list)

    @computed_field
    def total_cost_usd(self) -> float:
        """Calculates total real-time USD cost across all nodes."""
        return sum(t.cost_usd for t in self.telemetry)

    @computed_field
    def critical_finding_count(self) -> int:
        """Calculates the frequency of critical findings across all branches."""
        all_findings = self.security_findings + self.architecture_findings + self.style_findings
        return sum(1 for f in all_findings if f.severity.lower() == "critical")
