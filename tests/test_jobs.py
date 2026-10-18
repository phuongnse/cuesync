"""Tests for the background job table and the placeholder subtitle."""

from __future__ import annotations

import asyncio

from cuesync.jobs import FINISHED_TTL_S, Jobs, JobState
from cuesync.placeholder import MESSAGES, placeholder_cues
from cuesync.search import encode_key
from cuesync.sources.base import Candidate
from cuesync.sync import Correction, Verdict

SYNCED = Correction(Verdict.SYNCED, 1.0, 2000, 5, 10.0)
ONE = Candidate("fake", "1", "vie", "")
TWO = Candidate("fake", "2", "eng", "")


def test_submitting_the_same_subtitle_twice_runs_the_work_once():
    calls = []

    async def work(candidates):
        calls.append(list(candidates))
        await asyncio.sleep(0.01)
        return {c: SYNCED for c in candidates}

    async def main():
        jobs = Jobs()
        jobs.submit("abc", [ONE], work)
        jobs.submit("abc", [ONE], work)
        jobs.submit("abc", [ONE, TWO], work)
        await jobs.join()
        return jobs

    jobs = asyncio.run(main())
    assert calls == [[ONE], [TWO]]
    job = jobs.get("abc", encode_key(ONE))
    assert (job.state, job.correction, job.language) == (JobState.DONE, SYNCED, "vie")
    assert [j.subtitle_key for j in jobs.for_video("abc")] == [encode_key(ONE), encode_key(TWO)]
    assert jobs.for_video("other") == []


def test_candidates_of_one_submit_share_one_task():
    calls = []

    async def work(candidates):
        calls.append(list(candidates))
        return {c: SYNCED for c in candidates}

    async def main():
        jobs = Jobs()
        jobs.submit("abc", [ONE, TWO], work)
        await jobs.join()

    asyncio.run(main())
    assert calls == [[ONE, TWO]]


def test_job_is_pending_then_running_then_done():
    states = []

    async def main():
        jobs = Jobs()
        release = asyncio.Event()

        async def work(candidates):
            await release.wait()
            return {c: SYNCED for c in candidates}

        jobs.submit("abc", [ONE], work)
        job = jobs.get("abc", encode_key(ONE))
        states.append(job.state)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        states.append(job.state)
        release.set()
        await jobs.join()
        states.append(job.state)

    asyncio.run(main())
    assert states == [JobState.PENDING, JobState.RUNNING, JobState.DONE]


def test_the_limit_makes_jobs_of_two_videos_run_one_after_the_other():
    events = []

    async def work(candidates):
        events.append(("start", candidates[0].source_id))
        await asyncio.sleep(0.02)
        events.append(("end", candidates[0].source_id))
        return {c: SYNCED for c in candidates}

    async def main():
        jobs = Jobs(limit=1)
        jobs.submit("one", [ONE], work)
        jobs.submit("two", [TWO], work)
        await jobs.join()

    asyncio.run(main())
    assert events == [("start", "1"), ("end", "1"), ("start", "2"), ("end", "2")]


def test_job_that_takes_too_long_fails_with_a_timeout():
    async def work(candidates):
        await asyncio.sleep(5)
        return {}

    async def main():
        jobs = Jobs(timeout=0.05)
        jobs.submit("abc", [ONE], work)
        await jobs.join()
        return jobs.get("abc", encode_key(ONE))

    job = asyncio.run(main())
    assert (job.state, job.error, job.correction) == (JobState.FAILED, "timeout", None)
    assert job.finished_at is not None


def test_exception_fails_the_job_with_its_type_name_only(caplog):
    async def work(candidates):
        raise ValueError("secret detail")

    async def main():
        jobs = Jobs()
        jobs.submit("abc", [ONE], work)
        await jobs.join()
        return jobs.get("abc", encode_key(ONE))

    job = asyncio.run(main())
    assert (job.state, job.error) == (JobState.FAILED, "ValueError")
    assert "ValueError" in caplog.text
    assert "secret detail" not in caplog.text


def test_subtitle_missing_from_the_result_fails_as_unavailable():
    async def work(candidates):
        return {ONE: SYNCED}

    async def main():
        jobs = Jobs()
        jobs.submit("abc", [ONE, TWO], work)
        await jobs.join()
        return jobs

    jobs = asyncio.run(main())
    assert jobs.get("abc", encode_key(ONE)).state is JobState.DONE
    missing = jobs.get("abc", encode_key(TWO))
    assert (missing.state, missing.error) == (JobState.FAILED, "unavailable")


def test_finished_jobs_are_forgotten_after_the_ttl():
    now = [100.0]

    async def failing(candidates):
        raise RuntimeError

    async def main():
        jobs = Jobs(clock=lambda: now[0])
        jobs.submit("abc", [ONE], failing)
        await jobs.join()
        key = encode_key(ONE)
        assert jobs.get("abc", key).state is JobState.FAILED
        now[0] += FINISHED_TTL_S
        assert jobs.get("abc", key) is not None
        now[0] += 1
        assert jobs.get("abc", key) is None
        assert jobs.for_video("abc") == []

        async def work(candidates):
            return {c: SYNCED for c in candidates}

        jobs.submit("abc", [ONE], work)
        await jobs.join()
        return jobs.get("abc", key)

    assert asyncio.run(main()).state is JobState.DONE


def test_close_cancels_running_jobs():
    async def work(candidates):
        await asyncio.sleep(60)
        return {}

    async def main():
        jobs = Jobs()
        jobs.submit("abc", [ONE], work)
        await asyncio.sleep(0)
        await jobs.close()
        await jobs.join()

    asyncio.run(main())


def test_placeholder_is_one_long_cue_in_the_language():
    (cue,) = placeholder_cues("vie")
    assert cue.text == MESSAGES["vie"]
    assert cue.text != MESSAGES["eng"]
    assert (cue.start_ms, cue.end_ms) == (0, 10 * 3600 * 1000)


def test_placeholder_falls_back_to_english():
    assert placeholder_cues("kor")[0].text == MESSAGES["eng"]
    assert "Reopen the subtitle menu" in MESSAGES["eng"]
