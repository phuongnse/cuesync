"""Tests for re-timed subtitles in the Stremio list. Every stage is a fake."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from cuesync.app import create_app
from cuesync.audio import AudioTrack, AudioWindow, MediaInfo
from cuesync.cache import FAILURE_RETRY_S, Cache
from cuesync.config import UserConfig, encode_config
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
        return [AudioWindow(600.0, bytes(3200))]

    def align(self, windows, cues):
        self.align_calls += 1
        return self.correction

    def stages(self) -> Stages:
        return Stages(self.find_stream, self.debrid, self.probe, self.extract_windows, self.align)


def make_client(
    source: FakeSource, fake: FakeStages, cache: Cache | None = None, **kwargs
) -> TestClient:
    app = create_app(
        source_factory=lambda config, http: [source], stages=fake.stages(), cache=cache, **kwargs
    )
    return TestClient(app)


def entries_of(client: TestClient, token: str = TOKEN, path: str = PATH) -> list[dict]:
    response = client.get(f"/{token}/{path}")
    assert response.status_code == 200
    return response.json()["subtitles"]


def test_retimed_entry_is_listed_before_the_originals():
    source = FakeSource(candidates=[fake_candidate("1", "vie"), fake_candidate("2", "vie")])
    entries = entries_of(make_client(source, FakeStages()))
    assert [e["id"] for e in entries[:1]] == ["cuesync-vie-1"]
    assert entries[0]["lang"] == "vie"
    originals = entries[1:]
    assert [e["id"] for e in originals] == [
        encode_key(fake_candidate("1", "vie")),
        encode_key(fake_candidate("2", "vie")),
    ]
    # Only the top-ranked subtitle is downloaded for re-timing.
    assert [d.source_id for d in source.downloads] == ["1"]


def test_retimed_entry_serves_the_shifted_srt():
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    response = client.get(entries[0]["url"].removeprefix("http://testserver"))
    assert response.status_code == 200
    assert response.content.decode() == "1\n00:00:03,000 --> 00:00:04,500\nHello there\n\n"


def test_original_is_served_from_the_cache_after_retiming():
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, FakeStages())
    entries = entries_of(client)
    response = client.get(entries[1]["url"].removeprefix("http://testserver"))
    assert response.content.decode() == "1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n"
    assert len(source.downloads) == 1


def test_retimed_link_works_from_a_fresh_app():
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, FakeStages()))
    path = entries[0]["url"].removeprefix("http://testserver")
    first = make_client(source, FakeStages()).get(path)
    second = make_client(FakeSource(), FakeStages()).get(path)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.content.decode().startswith("1\n00:00:03,000")


def test_failed_sync_lists_the_originals_only():
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, FakeStages(correction=FAILED)))
    assert [e["id"] for e in entries] == [encode_key(fake_candidate("1", "vie"))]


def test_unchanged_sync_lists_the_originals_only():
    unchanged = Correction(Verdict.UNCHANGED, 1.0, 0, 5, 1.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, FakeStages(correction=unchanged)))
    assert len(entries) == 1


def test_no_debrid_lists_the_originals_only_without_a_stream_lookup():
    fake = FakeStages(debrid_enabled=False)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, fake), NO_DEBRID)
    assert len(entries) == 1
    assert fake.stream_calls == 0
    assert source.downloads == []


def test_missing_stream_lists_the_originals_only():
    fake = FakeStages(stream=None)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, fake))
    assert len(entries) == 1
    assert source.downloads == []


def test_request_without_file_details_lists_the_originals_only():
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    response = make_client(source, fake).get(f"/{TOKEN}/subtitles/movie/tt1375666.json")
    assert len(response.json()["subtitles"]) == 1
    assert fake.stream_calls == 0


def test_slow_audio_stage_hits_the_budget():
    fake = FakeStages(extract_delay=5.0)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    client = make_client(source, fake, retime_budget=0.05)
    started = time.monotonic()
    entries = entries_of(client)
    assert time.monotonic() - started < 1.0
    assert len(entries) == 1


def test_download_failure_lists_the_originals_only():
    source = FakeSource(candidates=[fake_candidate("1", "vie")], error="quota")
    entries = entries_of(make_client(source, FakeStages()))
    assert len(entries) == 1


def test_stage_crash_lists_the_originals_only(caplog):
    class Boom(FakeStages):
        async def find_stream(self, client, video, filename, size):
            raise RuntimeError("secret detail")

    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries = entries_of(make_client(source, Boom()))
    assert len(entries) == 1
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


def test_audio_is_read_once_for_every_language():
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie"), fake_candidate("2", "eng")])
    entries = entries_of(make_client(source, fake), TWO_LANGUAGES)
    assert [e["id"] for e in entries[:2]] == ["cuesync-vie-1", "cuesync-eng-1"]
    assert [e["lang"] for e in entries[:2]] == ["vie", "eng"]
    assert len(entries) == 4
    assert fake.extract_calls == 1
    assert fake.align_calls == 2


def test_second_request_from_a_fresh_app_is_answered_from_the_cache(tmp_path):
    fake = FakeStages()
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    first = entries_of(make_client(source, fake, Cache(tmp_path)), path=SIZED_PATH)
    calls = (fake.stream_calls, fake.extract_calls, fake.align_calls, len(source.downloads))
    assert calls == (1, 1, 1, 1)
    second = entries_of(make_client(source, fake, Cache(tmp_path)), path=SIZED_PATH)
    assert [e["id"] for e in second[:1]] == ["cuesync-vie-1"]
    assert second == first
    assert (fake.stream_calls, fake.extract_calls, fake.align_calls, len(source.downloads)) == calls


def test_stored_sync_result_is_used_without_a_debrid_service(tmp_path):
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries_of(make_client(source, FakeStages(), Cache(tmp_path)))
    fake = FakeStages(debrid_enabled=False)
    entries = entries_of(make_client(source, fake, Cache(tmp_path)), NO_DEBRID)
    assert [e["id"] for e in entries[:1]] == ["cuesync-vie-1"]
    assert fake.stream_calls == 0


def test_stored_timeline_aligns_a_new_subtitle_without_reading_the_audio(tmp_path):
    fake = FakeStages()
    first = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries_of(make_client(first, fake, Cache(tmp_path)), path=SIZED_PATH)
    second = FakeSource(name="other", candidates=[Candidate("other", "2", "vie", "")])
    entries = entries_of(make_client(second, fake, Cache(tmp_path)), path=SIZED_PATH)
    assert [e["id"] for e in entries[:1]] == ["cuesync-vie-1"]
    assert [d.source_id for d in second.downloads] == ["2"]
    assert fake.stream_calls == 1
    assert fake.extract_calls == 1
    assert fake.align_calls == 2


def test_timeline_is_not_stored_without_a_video_size(tmp_path):
    fake = FakeStages()
    first = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries_of(make_client(first, fake, Cache(tmp_path)))
    second = FakeSource(name="other", candidates=[Candidate("other", "2", "vie", "")])
    entries_of(make_client(second, fake, Cache(tmp_path)))
    assert fake.extract_calls == 2


def test_failed_sync_is_not_tried_again_for_a_day(tmp_path):
    now = [1000.0]
    cache = Cache(tmp_path, clock=lambda: now[0])
    fake = FakeStages(correction=FAILED)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    entries_of(make_client(source, fake, cache))
    assert fake.align_calls == 1
    now[0] += FAILURE_RETRY_S - 1
    entries = entries_of(make_client(source, fake, cache))
    assert [e["id"] for e in entries] == [encode_key(fake_candidate("1", "vie"))]
    assert (fake.stream_calls, fake.align_calls) == (1, 1)
    now[0] += 1
    entries_of(make_client(source, fake, cache))
    assert (fake.stream_calls, fake.align_calls) == (2, 2)


def test_stage_failures_are_not_stored(tmp_path):
    cache = Cache(tmp_path)
    source = FakeSource(candidates=[fake_candidate("1", "vie")])
    assert len(entries_of(make_client(source, FakeStages(stream=None), cache))) == 1
    entries = entries_of(make_client(source, FakeStages(), cache))
    assert [e["id"] for e in entries[:1]] == ["cuesync-vie-1"]
