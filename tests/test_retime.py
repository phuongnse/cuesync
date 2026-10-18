"""Tests for re-timed subtitles and their background jobs. Every stage is a fake."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from cuesync.app import create_app
from cuesync.audio import AudioTrack, AudioWindow, MediaInfo
from cuesync.cache import FAILURE_RETRY_S, Cache
from cuesync.config import UserConfig, encode_config
from cuesync.placeholder import MESSAGES
from cuesync.retime import Stages
from cuesync.search import encode_key
from cuesync.sources.base import Candidate
from cuesync.stream import StreamRef
from cuesync.sync import Correction, Verdict
from test_app import FakeSource, fake_candidate

TOKEN = encode_config(
    UserConfig(languages=["vie"], debrid_provider="realdebrid", debrid_api_key="k")
)
TWO_LANGUAGES = encode_config(
    UserConfig(languages=["vie", "eng"], debrid_provider="realdebrid", debrid_api_key="k")
)
NO_DEBRID = encode_config(UserConfig(languages=["vie"]))
PATH = "subtitles/movie/tt1375666/videoHash=abc&filename=a.mkv.json"
SIZED_PATH = "subtitles/movie/tt1375666/videoHash=abc&videoSize=123&filename=a.mkv.json"
SYNCED = Correction(Verdict.SYNCED, 1.0, 2000, 5, 10.0)
STREAM = StreamRef("a" * 40, 0, "a.mkv", 123)
FAILED = Correction(Verdict.FAILED, 1.0, 0, 0, 0.0)


class FakeDebrid:
    name = "fake"

    async def direct_url(self, stream: StreamRef) -> str | None:
        return "https://x/file"


@dataclass
class FakeStages:
    correction: Correction = SYNCED
    stream: StreamRef | None = STREAM
    debrid_enabled: bool = True
    extract_delay: float = 0.0
    gate: threading.Event | None = None
    stream_calls: int = 0
    extract_calls: int = 0
    align_calls: int = 0
    aligned: list = field(default_factory=list)

    async def find_stream(self, client, video, filename, size):
        self.stream_calls += 1
        return self.stream

    def debrid(self, config, client):
        return FakeDebrid() if self.debrid_enabled else None

    async def probe(self, url):
        return MediaInfo(3600.0, (AudioTrack(0, None, True),))

    async def extract_windows(self, url, duration, *, track):
        self.extract_calls += 1
        await asyncio.sleep(self.extract_delay)
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 10)
        return [AudioWindow(600.0, bytes(3200))]

    def align(self, windows, cues):
        self.align_calls += 1
        return self.correction

    def stages(self) -> Stages:
        return Stages(self.find_stream, self.debrid, self.probe, self.extract_windows, self.align)


@pytest.fixture
def make_client():
    """Build clients that stay entered, so one event loop outlives the requests."""
    with ExitStack() as stack:

        def make(
            source: FakeSource, fake: FakeStages, cache: Cache | None = None, **kwargs
        ) -> TestClient:
            app = create_app(
                source_factory=lambda config, http: [source],
                stages=fake.stages(),
                cache=cache,
                **kwargs,
            )
            return stack.enter_context(TestClient(app))

        yield make


def settle(client: TestClient) -> None:
    """Wait until every background job has finished."""
    client.portal.call(client.app.state.jobs.join)


def entries_of(client: TestClient, token: str = TOKEN, path: str = PATH) -> list[dict]:
    response = client.get(f"/{token}/{path}")
    assert response.status_code == 200
    return response.json()["subtitles"]


def path_of(entry: dict) -> str:
    return entry["url"].removeprefix("http://testserver")


def ids_of(entries: list[dict]) -> list[str]:
    return [e["id"] for e in entries]


def status_of(client: TestClient, video_hash: str = "abc", token: str = TOKEN) -> dict:
    response = client.get(f"/{token}/status/{video_hash}.json")
    assert response.status_code == 200
    return response.json()


def original(source_id: str, language: str = "vie") -> str:
    return encode_key(fake_candidate(source_id, language))


def test_retimed_entry_is_listed_before_the_originals(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie"), fake_candidate("2", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    assert ids_of(entries) == ["cuesync-vie-1", original("1"), original("2")]
    assert entries[0]["lang"] == "vie"
    settle(client)
    assert ids_of(entries_of(client)) == ids_of(entries)
    # Only the top-ranked subtitle is downloaded for re-timing.
    assert [d.source_id for d in source.downloads] == ["1"]


def test_retimed_entry_serves_the_shifted_srt(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    settle(client)
    response = client.get(path_of(entries[0]))
    assert response.status_code == 200
    assert response.content.decode() == "1\n00:00:03,000 --> 00:00:04,500\nHello there\n\n"


def test_original_is_served_from_the_cache_after_retiming(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    settle(client)
    response = client.get(path_of(entries[1]))
    assert response.content.decode() == "1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n"
    assert len(source.downloads) == 1


def test_retimed_link_works_from_a_fresh_app_with_the_same_cache(make_client, tmp_path):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = make_client(source, FakeStages(), Cache(tmp_path))
    entries = entries_of(first)
    settle(first)
    path = path_of(entries[0])
    second = make_client(FakeSource(), FakeStages(), Cache(tmp_path))
    response = second.get(path)
    assert response.status_code == 200
    assert response.content == first.get(path).content
    assert response.content.decode().startswith("1\n00:00:03,000")


def test_retimed_link_without_a_job_or_stored_result_serves_the_original(make_client, tmp_path):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, FakeStages(), Cache(tmp_path / "first")))
    response = make_client(source, FakeStages(), Cache(tmp_path / "second")).get(
        path_of(entries[0])
    )
    assert response.status_code == 200
    assert response.content.decode().startswith("1\n00:00:01,000")


def test_failed_sync_shows_a_placeholder_then_lists_the_originals_only(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages(correction=FAILED))
    assert ids_of(entries_of(client)) == ["cuesync-vie-1", original("1")]
    settle(client)
    assert ids_of(entries_of(client)) == [original("1")]


def test_unchanged_sync_shows_a_placeholder_then_lists_the_originals_only(make_client):
    unchanged = Correction(Verdict.UNCHANGED, 1.0, 0, 5, 1.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages(correction=unchanged))
    entries = entries_of(client)
    assert len(entries) == 2
    settle(client)
    assert len(entries_of(client)) == 1
    job = status_of(client)["jobs"][0]
    assert (job["state"], job["verdict"]) == ("done", "unchanged")
    # The unchanged result is not shifted, so the old link serves the original.
    assert client.get(path_of(entries[0])).content.decode().startswith("1\n00:00:01,000")


def test_no_debrid_lists_the_originals_only_without_a_stream_lookup(make_client):
    fake = FakeStages(debrid_enabled=False)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    assert len(entries_of(client, NO_DEBRID)) == 1
    settle(client)
    assert fake.stream_calls == 0
    assert source.downloads == []
    assert status_of(client, token=NO_DEBRID)["jobs"] == []


def test_missing_stream_fails_the_job_and_lists_the_originals_only(make_client):
    fake = FakeStages(stream=None)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    assert len(entries_of(client)) == 2
    settle(client)
    assert len(entries_of(client)) == 1
    assert [(j["state"], j["error"]) for j in status_of(client)["jobs"]] == [
        ("failed", "unavailable")
    ]
    assert source.downloads == []
    assert fake.stream_calls == 1


def test_request_without_file_details_lists_the_originals_only(make_client):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    response = client.get(f"/{TOKEN}/subtitles/movie/tt1375666/videoHash=abc.json")
    assert len(response.json()["subtitles"]) == 1
    assert len(client.get(f"/{TOKEN}/subtitles/movie/tt1375666.json").json()["subtitles"]) == 1
    settle(client)
    assert fake.stream_calls == 0


@pytest.mark.parametrize(
    "extra", ["filename=a.mkv", "videoHash=a%2Fb&filename=a.mkv", f"videoHash={'a' * 65}&size=1"]
)
def test_request_without_a_plain_video_hash_lists_the_originals_only(make_client, extra):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    assert len(entries_of(client, path=f"subtitles/movie/tt1375666/{extra}.json")) == 1
    settle(client)
    assert fake.stream_calls == 0


def test_slow_audio_stage_does_not_hold_up_the_list(make_client):
    fake = FakeStages(extract_delay=5.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    started = time.monotonic()
    entries = entries_of(client)
    assert time.monotonic() - started < 1.0
    assert ids_of(entries) == ["cuesync-vie-1", original("1")]


def test_job_that_runs_too_long_fails_with_a_timeout(make_client):
    fake = FakeStages(extract_delay=5.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, job_timeout=0.05)
    assert len(entries_of(client)) == 2
    settle(client)
    jobs = status_of(client)["jobs"]
    assert [(j["state"], j["error"], j["verdict"]) for j in jobs] == [("failed", "timeout", None)]
    assert ids_of(entries_of(client)) == [original("1")]
    assert fake.stream_calls == 1


def test_download_failure_lists_the_originals_only(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")], error="quota")
    client = make_client(source, FakeStages())
    entries_of(client)
    settle(client)
    assert len(entries_of(client)) == 1


def test_stage_crash_fails_the_job_without_leaking_details(make_client, caplog):
    class Boom(FakeStages):
        async def find_stream(self, client, video, filename, size):
            raise RuntimeError("secret detail")

    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, Boom())
    entries_of(client)
    settle(client)
    assert len(entries_of(client)) == 1
    assert [j["error"] for j in status_of(client)["jobs"]] == ["RuntimeError"]
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    assert "secret detail" not in client.get(f"/{TOKEN}/status/abc.json").text


def test_audio_is_read_once_for_every_language(make_client):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie"), fake_candidate("2", "eng")])
    client = make_client(source, fake)
    entries = entries_of(client, TWO_LANGUAGES)
    settle(client)
    assert ids_of(entries_of(client, TWO_LANGUAGES))[:2] == ["cuesync-vie-1", "cuesync-eng-1"]
    assert [e["lang"] for e in entries[:2]] == ["vie", "eng"]
    assert len(entries) == 4
    assert fake.extract_calls == 1
    assert fake.align_calls == 2


def test_concurrent_requests_share_one_job(make_client):
    gate = threading.Event()
    fake = FakeStages(gate=gate)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    first = entries_of(client)
    second = entries_of(client)
    gate.set()
    settle(client)
    assert first == second
    assert first[0]["id"] == "cuesync-vie-1"
    assert (fake.stream_calls, fake.extract_calls, fake.align_calls) == (1, 1, 1)
    assert len(status_of(client)["jobs"]) == 1


def test_placeholder_is_served_until_the_job_finishes(make_client):
    gate = threading.Event()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages(gate=gate))
    path = path_of(entries_of(client)[0])
    pending = client.get(path)
    assert pending.status_code == 200
    assert pending.headers["cache-control"] == "no-store"
    assert pending.headers["content-type"].startswith("application/x-subrip")
    text = pending.content.decode()
    assert MESSAGES["vie"] in text
    assert "00:00:00,000 --> 10:00:00,000" in text
    assert status_of(client)["jobs"][0]["state"] in ("pending", "running")
    gate.set()
    settle(client)
    done = client.get(path)
    assert done.content.decode() == "1\n00:00:03,000 --> 00:00:04,500\nHello there\n\n"
    assert status_of(client)["jobs"][0]["state"] == "done"


def test_placeholder_in_a_language_without_a_translation_is_english(make_client):
    gate = threading.Event()
    source = FakeSource(candidates=[fake_candidate("1", "kor")])
    token = encode_config(
        UserConfig(languages=["kor"], debrid_provider="realdebrid", debrid_api_key="k")
    )
    client = make_client(source, FakeStages(gate=gate))
    path = path_of(entries_of(client, token)[0])
    try:
        assert MESSAGES["eng"] in client.get(path).content.decode()
    finally:
        gate.set()
    settle(client)


def test_status_lists_the_job_with_its_correction(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries_of(client)
    settle(client)
    assert status_of(client) == {
        "videoHash": "abc",
        "jobs": [
            {
                "subtitle": original("1"),
                "lang": "vie",
                "state": "done",
                "verdict": "synced",
                "scale": 1.0,
                "offsetMs": 2000,
                "error": None,
            }
        ],
    }
    assert status_of(client, "other") == {"videoHash": "other", "jobs": []}


def test_status_needs_a_valid_config(make_client):
    client = make_client(FakeSource(), FakeStages())
    assert client.get("/not-a-config/status/abc.json").status_code == 400


def test_retimed_link_with_a_bad_key_or_hash_is_not_found(make_client):
    client = make_client(FakeSource(), FakeStages())
    assert client.get(f"/{TOKEN}/retimed/abc/garbage.srt").status_code == 404
    assert client.get(f"/{TOKEN}/retimed/a-b/{original('1')}.srt").status_code == 404


def test_second_request_from_a_fresh_app_is_answered_from_the_cache(make_client, tmp_path):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, Cache(tmp_path))
    first = entries_of(client, path=SIZED_PATH)
    settle(client)
    calls = (fake.stream_calls, fake.extract_calls, fake.align_calls, len(source.downloads))
    assert calls == (1, 1, 1, 1)
    fresh = make_client(source, fake, Cache(tmp_path))
    second = entries_of(fresh, path=SIZED_PATH)
    settle(fresh)
    assert ids_of(second[:1]) == ["cuesync-vie-1"]
    assert second == first
    assert (fake.stream_calls, fake.extract_calls, fake.align_calls, len(source.downloads)) == calls
    assert status_of(fresh)["jobs"] == []


def test_stored_sync_result_is_used_without_a_debrid_service(make_client, tmp_path):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = make_client(source, FakeStages(), Cache(tmp_path))
    entries_of(first)
    settle(first)
    fake = FakeStages(debrid_enabled=False)
    entries = entries_of(make_client(source, fake, Cache(tmp_path)), NO_DEBRID)
    assert ids_of(entries[:1]) == ["cuesync-vie-1"]
    assert fake.stream_calls == 0


def test_stored_timeline_aligns_a_new_subtitle_without_reading_the_audio(make_client, tmp_path):
    fake = FakeStages()
    first = make_client(FakeSource(candidates=[fake_candidate("1", "vie")]), fake, Cache(tmp_path))
    entries_of(first, path=SIZED_PATH)
    settle(first)
    second_source = FakeSource(name="other", candidates=[Candidate("other", "2", "vie", "")])
    second = make_client(second_source, fake, Cache(tmp_path))
    entries = entries_of(second, path=SIZED_PATH)
    settle(second)
    assert ids_of(entries[:1]) == ["cuesync-vie-1"]
    assert [d.source_id for d in second_source.downloads] == ["2"]
    assert fake.stream_calls == 1
    assert fake.extract_calls == 1
    assert fake.align_calls == 2


def test_timeline_is_not_stored_without_a_video_size(make_client, tmp_path):
    fake = FakeStages()
    first = make_client(FakeSource(candidates=[fake_candidate("1", "vie")]), fake, Cache(tmp_path))
    entries_of(first)
    settle(first)
    other = FakeSource(name="other", candidates=[Candidate("other", "2", "vie", "")])
    second = make_client(other, fake, Cache(tmp_path))
    entries_of(second)
    settle(second)
    assert fake.extract_calls == 2


def test_failed_alignment_is_not_tried_again_for_a_day(make_client, tmp_path):
    now = [1000.0]
    cache = Cache(tmp_path, clock=lambda: now[0])
    fake = FakeStages(correction=FAILED)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, cache)
    entries_of(client)
    settle(client)
    assert fake.align_calls == 1
    now[0] += FAILURE_RETRY_S - 1
    second = make_client(source, fake, cache)
    assert ids_of(entries_of(second)) == [original("1")]
    settle(second)
    assert (fake.stream_calls, fake.align_calls) == (1, 1)
    now[0] += 1
    third = make_client(source, fake, cache)
    entries_of(third)
    settle(third)
    assert (fake.stream_calls, fake.align_calls) == (2, 2)


def test_stage_failures_are_not_stored(make_client, tmp_path):
    cache = Cache(tmp_path)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = make_client(source, FakeStages(stream=None), cache)
    entries_of(first)
    settle(first)
    assert len(entries_of(first)) == 1
    second = make_client(source, FakeStages(), cache)
    entries_of(second)
    settle(second)
    assert ids_of(entries_of(second)[:1]) == ["cuesync-vie-1"]
