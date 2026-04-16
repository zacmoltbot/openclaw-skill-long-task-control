from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ArtifactRecord:
    path: str
    kind: str = "artifact"
    label: str | None = None
    created_at: str = field(default_factory=now_iso)
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class FailureRecord:
    code: str
    summary: str
    retryable: bool = False
    facts: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=now_iso)


@dataclass
class WorkItem:
    item_id: str
    title: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "PENDING"
    attempts: int = 0
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    submitted_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    blocked_reason: str | None = None
    next_action: str | None = None
    execution_owner: str | None = None
    execution_claimed_at: str | None = None
    resume_count: int = 0


@dataclass
class JobState:
    job_id: str
    kind: str
    adapter: str
    mode: str = "serial"
    status: str = "PENDING"
    current_index: int = 0
    items: list[WorkItem] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    bridge: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = now_iso()

    def item_by_id(self, item_id: str) -> WorkItem:
        for item in self.items:
            if item.item_id == item_id:
                return item
        raise KeyError(item_id)

    def checkpoint_for_item(self, item: WorkItem, index: int | None = None) -> str:
        if item.payload.get("checkpoint"):
            return str(item.payload["checkpoint"])
        idx = self.items.index(item) if index is None else index
        return f"step-{idx + 1:02d}"

    def next_runnable(self) -> WorkItem | None:
        for idx in range(self.current_index, len(self.items)):
            item = self.items[idx]
            if item.status in {"PENDING", "RETRY", "RUNNING"}:
                self.current_index = idx
                return item
        for idx, item in enumerate(self.items):
            if item.status in {"PENDING", "RETRY", "RUNNING"}:
                self.current_index = idx
                return item
        return None


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def progress_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "progress.jsonl"

    def locks_dir(self) -> Path:
        path = self.root / "locks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def lock_file(self, job_id: str) -> Path:
        return self.locks_dir() / f"{job_id}.lock"

    def save(self, state: JobState) -> Path:
        job_dir = self.job_dir(state.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        state.touch()
        payload = asdict(state)
        self.job_file(state.job_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return self.job_file(state.job_id)

    def append_progress(self, job_id: str, event: dict[str, Any]) -> None:
        path = self.progress_file(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {"at": now_iso(), **event}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def load(self, job_id: str) -> JobState:
        data = json.loads(self.job_file(job_id).read_text())
        items = [
            WorkItem(
                **{
                    **item,
                    "artifacts": [ArtifactRecord(**a) for a in item.get("artifacts", [])],
                    "failures": [FailureRecord(**fr) for fr in item.get("failures", [])],
                }
            )
            for item in data.get("items", [])
        ]
        artifacts = [ArtifactRecord(**a) for a in data.get("artifacts", [])]
        return JobState(
            job_id=data["job_id"],
            kind=data["kind"],
            adapter=data["adapter"],
            mode=data.get("mode", "serial"),
            status=data.get("status", "PENDING"),
            current_index=data.get("current_index", 0),
            items=items,
            completed=data.get("completed", []),
            failed=data.get("failed", []),
            blocked_reason=data.get("blocked_reason"),
            artifacts=artifacts,
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            bridge=data.get("bridge", {}) or {},
            execution=data.get("execution", {}) or {},
        )

    def try_acquire_lock(self, job_id: str, *, owner: str) -> tuple[bool, dict[str, Any]]:
        path = self.lock_file(job_id)
        payload = {"job_id": job_id, "owner": owner, "pid": os.getpid(), "acquired_at": now_iso()}
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = {"owner": "unknown", "raw": path.read_text(errors="ignore") if path.exists() else ""}
            return False, existing
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return True, payload

    def release_lock(self, job_id: str, *, owner: str) -> bool:
        path = self.lock_file(job_id)
        if not path.exists():
            return False
        try:
            current = json.loads(path.read_text())
        except Exception:
            current = {}
        if current.get("owner") not in {None, owner}:
            return False
        path.unlink(missing_ok=True)
        return True
