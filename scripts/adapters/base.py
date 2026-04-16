from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class AdapterResult:
    status: str
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    next_action: str | None = None
    blocked_reason: str | None = None


class LongTaskAdapter(Protocol):
    """Base contract for domain adapters used by the execution plane.

    Adapters must keep domain-specific logic out of the core runner.
    The runner owns queue/state/progress; the adapter owns submit/observe/collect semantics.
    """

    name: str

    def plan(self, job_spec: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        ...

    def prepare(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        ...

    def submit(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        ...

    def observe(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        ...

    def collect(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        ...

    def finalize(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        ...

    def can_resume(self, item_state: dict[str, Any]) -> bool:
        return True

    def suggest_retry(self, item_state: dict[str, Any]) -> dict[str, Any]:
        return {"retryable": False}

    def is_human_gate(self, item_state: dict[str, Any]) -> bool:
        return False
