"""Background sync jobs, one per video and subtitle.

Fetching a re-timed subtitle starts a job and returns at once. The job runs the slow work
(stream lookup, audio reads, alignment) in the background, so Stremio is never kept waiting.
A job's task may cover more than one subtitle, and each job is finished as soon as its own
result is known. Jobs live in memory only. A restart drops them and the next fetch starts them
again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from cuesync.search import encode_key
from cuesync.sources.base import Candidate
from cuesync.sync import Correction

# Each job reads audio from the user's Real-Debrid link, so only a few run at once.
JOB_LIMIT = 2
# A job that takes longer counts as failed.
JOB_TIMEOUT = 300.0
# A failed or finished job is remembered this long. A failure blocks a new try meanwhile.
FINISHED_TTL_S = 600

log = logging.getLogger(__name__)

Work = Callable[[list[Candidate]], Awaitable[dict[Candidate, Correction]]]


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    video_key: str
    subtitle_key: str
    language: str
    state: JobState = JobState.PENDING
    correction: Correction | None = None
    error: str | None = None
    finished_at: float | None = None


class Jobs:
    """The jobs of one app, deduplicated by `(video key, subtitle key)`."""

    def __init__(
        self,
        limit: int = JOB_LIMIT,
        timeout: float = JOB_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.timeout = timeout
        self._clock = clock
        self._semaphore: asyncio.Semaphore | None = None
        self._jobs: dict[tuple[str, str], Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def _purge(self) -> None:
        now = self._clock()
        expired = [
            ident
            for ident, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_S
        ]
        for ident in expired:
            del self._jobs[ident]

    def get(self, video_key: str, key: str) -> Job | None:
        self._purge()
        return self._jobs.get((video_key, key))

    def for_video(self, video_key: str) -> list[Job]:
        self._purge()
        return [job for job in self._jobs.values() if job.video_key == video_key]

    def submit(self, video_key: str, candidates: Sequence[Candidate], work: Work) -> None:
        """Start one task for the candidates that have no job yet."""
        self._purge()
        fresh = [
            candidate
            for candidate in candidates
            if (video_key, encode_key(candidate)) not in self._jobs
        ]
        if not fresh:
            return
        jobs = []
        for candidate in fresh:
            key = encode_key(candidate)
            job = Job(video_key, key, candidate.language)
            self._jobs[(video_key, key)] = job
            jobs.append(job)
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.limit)
        task = asyncio.create_task(self._run(self._semaphore, fresh, jobs, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(
        self,
        semaphore: asyncio.Semaphore,
        candidates: list[Candidate],
        jobs: list[Job],
        work: Work,
    ) -> None:
        async with semaphore:
            for job in jobs:
                job.state = JobState.RUNNING
            try:
                async with asyncio.timeout(self.timeout):
                    result = await work(candidates)
            except TimeoutError:
                self._fail(jobs, "timeout")
                log.warning("sync job timed out")
            except Exception as exc:
                self._fail(jobs, type(exc).__name__)
                log.warning("sync job failed: %s", type(exc).__name__)
            else:
                for candidate, job in zip(candidates, jobs, strict=True):
                    if job.finished_at is None:
                        self._finish(job, result.get(candidate))

    def finish(self, video_key: str, key: str, correction: Correction | None) -> None:
        """Finish one job before its task ends. Unknown and finished jobs are left alone."""
        job = self._jobs.get((video_key, key))
        if job is not None and job.finished_at is None:
            self._finish(job, correction)

    def _finish(self, job: Job, correction: Correction | None) -> None:
        if correction is None:
            job.state = JobState.FAILED
            job.error = "unavailable"
        else:
            job.state = JobState.DONE
            job.correction = correction
        job.finished_at = self._clock()

    def _fail(self, jobs: list[Job], error: str) -> None:
        for job in jobs:
            if job.finished_at is None:
                job.state = JobState.FAILED
                job.error = error
                job.finished_at = self._clock()

    async def join(self) -> None:
        """Wait for the running tasks to finish. For tests."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.wait(pending)

    async def close(self) -> None:
        """Cancel the running tasks and wait for them."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
