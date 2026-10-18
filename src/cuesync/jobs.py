"""Background sync jobs, one per video and subtitle.

A subtitles request starts a job and returns at once. The job runs the slow work (stream
lookup, audio reads, alignment) in the background, so Stremio is never kept waiting. Jobs live
in memory only. A restart drops them and the next request starts them again.
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

# Each job reads audio from the user's debrid link, so only a few run at once.
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
    video_hash: str
    subtitle_key: str
    language: str
    state: JobState = JobState.PENDING
    correction: Correction | None = None
    error: str | None = None
    finished_at: float | None = None


class Jobs:
    """The jobs of one app, deduplicated by `(video hash, subtitle key)`."""

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

    def get(self, video_hash: str, key: str) -> Job | None:
        self._purge()
        return self._jobs.get((video_hash, key))

    def for_video(self, video_hash: str) -> list[Job]:
        self._purge()
        return [job for job in self._jobs.values() if job.video_hash == video_hash]

    def submit(self, video_hash: str, candidates: Sequence[Candidate], work: Work) -> None:
        """Start one task for the candidates that have no job yet.

        The task runs `work` once for all of them, so the audio is read once for every language.
        """
        self._purge()
        fresh = [
            candidate
            for candidate in candidates
            if (video_hash, encode_key(candidate)) not in self._jobs
        ]
        if not fresh:
            return
        jobs = []
        for candidate in fresh:
            key = encode_key(candidate)
            job = Job(video_hash, key, candidate.language)
            self._jobs[(video_hash, key)] = job
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
                finished_at = self._clock()
                for candidate, job in zip(candidates, jobs, strict=True):
                    correction = result.get(candidate)
                    if correction is None:
                        job.state = JobState.FAILED
                        job.error = "unavailable"
                    else:
                        job.state = JobState.DONE
                        job.correction = correction
                    job.finished_at = finished_at

    def _fail(self, jobs: list[Job], error: str) -> None:
        finished_at = self._clock()
        for job in jobs:
            job.state = JobState.FAILED
            job.error = error
            job.finished_at = finished_at

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
