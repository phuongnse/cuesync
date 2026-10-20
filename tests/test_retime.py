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
from cuesync.search import decode_key, encode_key
from cuesync.sources.base import Candidate
from cuesync.stream import StreamRef
from cuesync.sync import Correction, Verdict
from test_app import FakeSource, fake_candidate

TOKEN = encode_config(UserConfig(debrid_api_key="k"))
NO_DEBRID = encode_config(UserConfig())
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
    probe_kwargs: list = field(default_factory=list)
    extract_kwargs: list = field(default_factory=list)

    async def find_stream(self, client, video, filename, size):
        self.stream_calls += 1
        return self.stream

    def debrid(self, config, client):
        return FakeDebrid() if self.debrid_enabled else None

    async def probe(self, url, **kwargs):
        self.probe_kwargs.append(kwargs)
        return MediaInfo(3600.0, (AudioTrack(0, None, True),))

    async def extract_windows(self, url, duration, *, track, **kwargs):
        self.extract_calls += 1
        self.extract_kwargs.append(kwargs)
        await asyncio.sleep(self.extract_delay)
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 10)
        return [AudioWindow(600.0, bytes(3200))]

    def align(self, windows, cues):
        self.align_calls += 1
        return self.correction

    def stages(self) -> Stages:
        return Stages(
            self.find_stream,
            self.debrid,
            self.probe,
            self.extract_windows,
            self.align,
        )


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


def pick_entry(client: TestClient, entry: dict):
    """Fetch an entry's link, as Stremio does when the user picks it."""
    return client.get(path_of(entry))


def ids_of(entries: list[dict]) -> list[str]:
    return [e["id"] for e in entries]


def status_of(client: TestClient, video_hash: str = "abc", token: str = TOKEN) -> dict:
    response = client.get(f"/{token}/status/{video_hash}.json")
    assert response.status_code == 200
    return response.json()


def original(source_id: str, language: str = "vie") -> str:
    return encode_key(fake_candidate(source_id, language))


SHIFTED = "1\n00:00:03,000 --> 00:00:04,500\nHello there\n\n"
UNSHIFTED = "1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n"


def test_every_candidate_is_listed_as_a_retimed_entry_without_starting_a_job(make_client):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate(str(i), "vie") for i in range(1, 6)])
    client = make_client(source, fake)
    entries = entries_of(client)
    assert len(entries) == 5
    assert len(set(ids_of(entries))) == 5
    for entry in entries:
        assert entry["url"] == f"http://testserver/{TOKEN}/retimed/abc/{entry['id']}.srt"
        assert entry["lang"] == "vie"
    settle(client)
    assert status_of(client)["jobs"] == []
    assert source.downloads == []
    assert fake.stream_calls == 0


def test_picked_entry_serves_the_placeholder_then_the_shifted_srt(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    first = pick_entry(client, entries[0])
    assert first.status_code == 200
    assert MESSAGES["vie"] in first.content.decode()
    settle(client)
    response = pick_entry(client, entries[0])
    assert response.status_code == 200
    assert response.content.decode() == SHIFTED
    assert ids_of(entries_of(client)) == ids_of(entries)


def test_only_the_picked_subtitle_is_synced(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie"), fake_candidate("2", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    pick_entry(client, entries[1])
    settle(client)
    assert [j["subtitle"] for j in status_of(client)["jobs"]] == [entries[1]["id"]]
    assert [d.source_id for d in source.downloads] == [decode_key(entries[1]["id"])[1]]


def test_original_is_served_from_the_cache_after_retiming(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    response = client.get(f"/{TOKEN}/sub/{entries[0]['id']}.srt")
    assert response.content.decode() == UNSHIFTED
    assert len(source.downloads) == 1


def test_retimed_link_works_from_a_fresh_app_with_the_same_cache(make_client, tmp_path):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = make_client(source, FakeStages(), Cache(tmp_path))
    entries = entries_of(first)
    pick_entry(first, entries[0])
    settle(first)
    path = path_of(entries[0])
    second = make_client(FakeSource(), FakeStages(), Cache(tmp_path))
    response = second.get(path)
    assert response.status_code == 200
    assert response.content == first.get(path).content
    assert response.content.decode().startswith("1\n00:00:03,000")


def test_retimed_link_of_an_unknown_video_serves_the_original_and_starts_no_job(
    make_client, tmp_path
):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, FakeStages(), Cache(tmp_path / "first")))
    fake = FakeStages()
    second = make_client(source, fake, Cache(tmp_path / "second"))
    response = pick_entry(second, entries[0])
    assert response.status_code == 200
    assert response.content.decode() == UNSHIFTED
    settle(second)
    assert status_of(second)["jobs"] == []
    assert fake.stream_calls == 0


def test_failed_sync_serves_the_original_and_keeps_the_entry(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages(correction=FAILED))
    entries = entries_of(client)
    assert MESSAGES["vie"] in pick_entry(client, entries[0]).content.decode()
    settle(client)
    assert ids_of(entries_of(client)) == [original("1")]
    assert pick_entry(client, entries[0]).content.decode() == UNSHIFTED
    job = status_of(client)["jobs"][0]
    assert (job["state"], job["verdict"]) == ("done", "failed")


def test_unchanged_sync_serves_the_original_and_keeps_the_entry(make_client):
    unchanged = Correction(Verdict.UNCHANGED, 1.0, 0, 5, 1.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages(correction=unchanged))
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    assert len(entries_of(client)) == 1
    job = status_of(client)["jobs"][0]
    assert (job["state"], job["verdict"]) == ("done", "unchanged")
    assert pick_entry(client, entries[0]).content.decode() == UNSHIFTED


def test_no_debrid_serves_the_original_without_a_job_or_stream_lookup(make_client):
    fake = FakeStages(debrid_enabled=False)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    entries = entries_of(client, NO_DEBRID)
    assert "/retimed/abc/" in entries[0]["url"]
    response = pick_entry(client, entries[0])
    assert response.content.decode() == UNSHIFTED
    settle(client)
    assert fake.stream_calls == 0
    assert status_of(client, token=NO_DEBRID)["jobs"] == []


def test_missing_stream_fails_the_job_and_serves_the_original(make_client):
    fake = FakeStages(stream=None)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    assert ids_of(entries_of(client)) == ids_of(entries)
    assert [(j["state"], j["error"]) for j in status_of(client)["jobs"]] == [
        ("failed", "unavailable")
    ]
    assert source.downloads == []
    assert fake.stream_calls == 1
    assert pick_entry(client, entries[0]).content.decode() == UNSHIFTED
    assert fake.stream_calls == 1


def test_request_without_file_details_lists_the_originals_only(make_client):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    response = client.get(f"/{TOKEN}/subtitles/movie/tt1375666/videoHash=abc.json")
    (entry,) = response.json()["subtitles"]
    assert entry["url"] == f"http://testserver/{TOKEN}/sub/{entry['id']}.srt"
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
    (entry,) = entries_of(client, path=f"subtitles/movie/tt1375666/{extra}.json")
    assert "/sub/" in entry["url"]
    settle(client)
    assert fake.stream_calls == 0


def test_slow_audio_stage_does_not_hold_up_the_list_or_the_pick(make_client):
    fake = FakeStages(extract_delay=5.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    started = time.monotonic()
    entries = entries_of(client)
    assert ids_of(entries) == [original("1")]
    assert MESSAGES["vie"] in pick_entry(client, entries[0]).content.decode()
    assert time.monotonic() - started < 1.0


def test_job_that_runs_too_long_fails_with_a_timeout(make_client):
    fake = FakeStages(extract_delay=5.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, job_timeout=0.05)
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    jobs = status_of(client)["jobs"]
    assert [(j["state"], j["error"], j["verdict"]) for j in jobs] == [("failed", "timeout", None)]
    assert ids_of(entries_of(client)) == [original("1")]
    assert pick_entry(client, entries[0]).content.decode() == UNSHIFTED
    assert fake.stream_calls == 1


def test_download_failure_fails_the_job_and_keeps_the_entry(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")], error="quota")
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    assert [(j["state"], j["error"]) for j in status_of(client)["jobs"]] == [
        ("failed", "unavailable")
    ]
    assert ids_of(entries_of(client)) == ids_of(entries)


def test_stage_crash_fails_the_job_without_leaking_details(make_client, caplog):
    class Boom(FakeStages):
        async def find_stream(self, client, video, filename, size):
            raise RuntimeError("secret detail")

    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, Boom())
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    assert len(entries_of(client)) == 1
    assert [j["error"] for j in status_of(client)["jobs"]] == ["RuntimeError"]
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    assert "secret detail" not in client.get(f"/{TOKEN}/status/abc.json").text


def test_second_fetch_while_the_job_runs_starts_no_second_job(make_client):
    gate = threading.Event()
    fake = FakeStages(gate=gate)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    entries = entries_of(client)
    first = pick_entry(client, entries[0])
    second = pick_entry(client, entries[0])
    assert MESSAGES["vie"] in first.content.decode()
    assert MESSAGES["vie"] in second.content.decode()
    gate.set()
    settle(client)
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
    assert done.content.decode() == SHIFTED
    assert status_of(client)["jobs"][0]["state"] == "done"


def test_status_lists_the_job_with_its_correction(make_client):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    pick_entry(client, entries_of(client)[0])
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


def test_remembered_videos_are_limited(make_client, monkeypatch):
    monkeypatch.setattr("cuesync.app.MAX_VIDEOS", 2)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    for name in ("one", "two", "three"):
        entries_of(client, path=f"subtitles/movie/tt1375666/videoHash={name}&filename=a.mkv.json")
    assert list(client.app.state.videos) == ["two", "three"]


def test_second_request_from_a_fresh_app_is_answered_from_the_cache(make_client, tmp_path):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, Cache(tmp_path))
    first = entries_of(client, path=SIZED_PATH)
    pick_entry(client, first[0])
    settle(client)
    calls = (fake.stream_calls, fake.extract_calls, fake.align_calls, len(source.downloads))
    assert calls == (1, 1, 1, 1)
    fresh = make_client(source, fake, Cache(tmp_path))
    second = entries_of(fresh, path=SIZED_PATH)
    assert pick_entry(fresh, second[0]).content.decode() == SHIFTED
    settle(fresh)
    assert second == first
    assert (fake.stream_calls, fake.extract_calls, fake.align_calls, len(source.downloads)) == calls
    assert status_of(fresh)["jobs"] == []


def test_stored_sync_result_is_used_without_a_debrid_service(make_client, tmp_path):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = make_client(source, FakeStages(), Cache(tmp_path))
    pick_entry(first, entries_of(first)[0])
    settle(first)
    fake = FakeStages(debrid_enabled=False)
    client = make_client(source, fake, Cache(tmp_path))
    entries = entries_of(client, NO_DEBRID)
    assert pick_entry(client, entries[0]).content.decode() == SHIFTED
    assert fake.stream_calls == 0


def test_second_subtitle_of_the_same_file_reuses_the_stored_timeline(make_client):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie"), fake_candidate("2", "vie")])
    client = make_client(source, fake)
    entries = entries_of(client, path=SIZED_PATH)
    pick_entry(client, entries[0])
    settle(client)
    pick_entry(client, entries[1])
    settle(client)
    assert len(status_of(client)["jobs"]) == 2
    assert fake.stream_calls == 1
    assert fake.extract_calls == 1
    assert fake.align_calls == 2
    assert len(source.downloads) == 2


def test_stored_timeline_aligns_a_new_subtitle_without_reading_the_audio(make_client, tmp_path):
    fake = FakeStages()
    first = make_client(FakeSource(candidates=[fake_candidate("1", "vie")]), fake, Cache(tmp_path))
    pick_entry(first, entries_of(first, path=SIZED_PATH)[0])
    settle(first)
    second_source = FakeSource(name="other", candidates=[Candidate("other", "2", "vie", "")])
    second = make_client(second_source, fake, Cache(tmp_path))
    entries = entries_of(second, path=SIZED_PATH)
    pick_entry(second, entries[0])
    settle(second)
    assert [d.source_id for d in second_source.downloads] == ["2"]
    assert fake.stream_calls == 1
    assert fake.extract_calls == 1
    assert fake.align_calls == 2


def test_timeline_is_not_stored_without_a_video_size(make_client, tmp_path):
    fake = FakeStages()
    first = make_client(FakeSource(candidates=[fake_candidate("1", "vie")]), fake, Cache(tmp_path))
    pick_entry(first, entries_of(first)[0])
    settle(first)
    other = FakeSource(name="other", candidates=[Candidate("other", "2", "vie", "")])
    second = make_client(other, fake, Cache(tmp_path))
    pick_entry(second, entries_of(second)[0])
    settle(second)
    assert fake.extract_calls == 2


def test_failed_alignment_is_not_tried_again_for_a_day(make_client, tmp_path):
    now = [1000.0]
    cache = Cache(tmp_path, clock=lambda: now[0])
    fake = FakeStages(correction=FAILED)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, cache)
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    assert fake.align_calls == 1
    now[0] += FAILURE_RETRY_S - 1
    second = make_client(source, fake, cache)
    assert ids_of(entries_of(second)) == [original("1")]
    assert pick_entry(second, entries[0]).content.decode() == UNSHIFTED
    settle(second)
    assert (fake.stream_calls, fake.align_calls) == (1, 1)
    now[0] += 1
    third = make_client(source, fake, cache)
    entries_of(third)
    pick_entry(third, entries[0])
    settle(third)
    assert (fake.stream_calls, fake.align_calls) == (2, 2)


def test_stage_failures_are_not_stored(make_client, tmp_path):
    cache = Cache(tmp_path)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = make_client(source, FakeStages(stream=None), cache)
    pick_entry(first, entries_of(first)[0])
    settle(first)
    assert [j["state"] for j in status_of(first)["jobs"]] == ["failed"]
    second = make_client(source, FakeStages(), cache)
    entries = entries_of(second)
    pick_entry(second, entries[0])
    settle(second)
    assert pick_entry(second, entries[0]).content.decode() == SHIFTED


def test_audio_is_read_through_the_debrid_link_only(make_client):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    entries = entries_of(client)
    assert ids_of(entries) == [original("1")]
    pick_entry(client, entries[0])
    settle(client)
    assert (fake.stream_calls, fake.extract_calls, fake.align_calls) == (1, 1, 1)
    assert fake.probe_kwargs == [{}]
    assert fake.extract_kwargs == [{}]


def test_debrid_without_a_link_serves_the_original(make_client):
    class NoLink(FakeDebrid):
        async def direct_url(self, stream):
            return None

    class Stages_(FakeStages):
        def debrid(self, config, client):
            return NoLink()

    fake = Stages_()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake)
    entries = entries_of(client)
    pick_entry(client, entries[0])
    settle(client)
    assert ids_of(entries_of(client)) == ids_of(entries)
    assert pick_entry(client, entries[0]).content.decode() == UNSHIFTED
    assert fake.extract_calls == 0
