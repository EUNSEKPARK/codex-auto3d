"""Job directories and their `job.json` state file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .util import Auto3DError, local_stamp, now_iso, read_json, relpath, slugify, write_json

JOB_FILE = "job.json"


@dataclass
class Job:
    dir: Path
    state: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.state.get("id") or self.dir.name)

    @property
    def rel(self) -> str:
        return relpath(self.dir)

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def stage(self, name: str) -> dict[str, Any]:
        return self.state.setdefault("stages", {}).setdefault(name, {"status": "pending"})

    def save(self) -> None:
        self.state["updatedAt"] = now_iso()
        write_json(self.dir / JOB_FILE, self.state)

    def set_status(self, status: str) -> None:
        self.state["status"] = status
        self.save()

    def add_usage(self, usage: dict[str, int]) -> None:
        total = self.state.setdefault("usage", {})
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value

    def add_error(self, message: str) -> None:
        self.state.setdefault("errors", []).append({"at": now_iso(), "message": message})


def create_job(settings: Settings, concept: str, *, name: str | None = None, extra: dict[str, Any] | None = None) -> Job:
    root = settings.work_root_path
    root.mkdir(parents=True, exist_ok=True)
    slug = slugify(name or concept, fallback="subject")
    job_id = f"{local_stamp()}-{slug}"
    job_dir = root / job_id
    counter = 1
    while job_dir.exists():
        counter += 1
        job_dir = root / f"{job_id}-{counter}"
    job_dir.mkdir(parents=True)
    state: dict[str, Any] = {
        "id": job_dir.name,
        "concept": concept.strip(),
        "name": name,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "status": "created",
        "settings": settings.as_dict(),
        "stages": {},
        "artifacts": {},
        "usage": {},
        "errors": [],
    }
    if extra:
        state.update(extra)
    job = Job(dir=job_dir, state=state)
    (job_dir / "concept.txt").write_text(concept.strip() + "\n", encoding="utf-8")
    job.save()
    return job


def load_job(path: Path) -> Job:
    path = Path(path).expanduser().resolve()
    if path.is_file() and path.name == JOB_FILE:
        path = path.parent
    state_path = path / JOB_FILE
    if not state_path.is_file():
        raise Auto3DError(f"not a job directory (no {JOB_FILE}): {path}")
    return Job(dir=path, state=read_json(state_path))


def list_jobs(root: Path) -> list[Job]:
    jobs: list[Job] = []
    if not root.is_dir():
        return jobs
    for candidate in sorted(root.iterdir()):
        if (candidate / JOB_FILE).is_file():
            try:
                jobs.append(load_job(candidate))
            except (Auto3DError, ValueError):
                continue
    return jobs
