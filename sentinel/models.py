"""Shared data structures."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}


@dataclass
class Message:
    role: str  # system | developer | user | assistant | tool
    text: str
    kind: str = "message"  # message | tool_call | tool_output | instructions
    name: str | None = None


@dataclass
class NormalizedRequest:
    """One Foundry request, whatever API it came through, in a common shape."""

    api: str  # chat_completions | inference | responses | agents.* | other
    model: str | None = None
    messages: list[Message] = field(default_factory=list)
    instructions: str | None = None  # top-level instructions (Responses / Agents)
    additional_instructions: str | None = None  # Agents run add-on
    tools: list[dict] = field(default_factory=list)
    max_output_tokens: int | None = None
    stream: bool = False
    agent_id: str | None = None
    thread_id: str | None = None
    previous_response_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def analyzable(self) -> bool:
        return self.api != "other"

    def system_texts(self) -> list[str]:
        out = []
        if self.instructions:
            out.append(self.instructions)
        out += [m.text for m in self.messages if m.role in ("system", "developer")]
        return out


@dataclass
class ResponseInfo:
    status: int
    latency_ms: int
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    object_id: str | None = None  # e.g. id of a created assistant / response / run
    run_status: str | None = None
    output_text: str | None = None


@dataclass
class Finding:
    rule: str
    severity: str  # info | warning | critical
    title: str
    detail: str
    suggestion: str
    evidence: dict[str, Any] = field(default_factory=dict)
    wasted_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Analysis:
    findings: list[Finding]
    breakdown: dict[str, int]
    est_input_tokens: int
    context_window: int
    health_score: int

    @property
    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda f: SEVERITY_RANK[f.severity]).severity
