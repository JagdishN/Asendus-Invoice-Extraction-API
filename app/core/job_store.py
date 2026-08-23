"""
Minimal in-memory job store for phase 1.

Database persistence is deferred, but the History screen still needs
*something* to read from -- this fills that gap without committing to a
schema prematurely. Swapping this for real Postgres/Supabase-backed
persistence in phase 2 should only require changing this module, since
routers/services talk to it through get_job / save_job / list_jobs only.

NOTE: this does not survive a process restart. That's an explicit, accepted
tradeoff for phase 1 (per client-agreed scope) -- flag clearly if that
becomes a problem before phase 2 lands.
"""

from __future__ import annotations

from uuid import UUID

from app.models.schemas import Job, JobSummary

_jobs: dict[UUID, Job] = {}


def save_job(job: Job) -> None:
    _jobs[job.job_id] = job


def get_job(job_id: UUID) -> Job | None:
    return _jobs.get(job_id)


def list_jobs(user_id: str | None = None) -> list[JobSummary]:
    jobs = _jobs.values()
    if user_id is not None:
        jobs = [j for j in jobs if j.user_id == user_id]
    return [
        JobSummary(
            job_id=j.job_id,
            original_filename=j.original_filename,
            status=j.status,
            invoice_count=len(j.invoice_groups),
            created_at=j.created_at,
            updated_at=j.updated_at,
        )
        for j in jobs
    ]
