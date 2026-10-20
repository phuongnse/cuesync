"""Tests for audio probing, track selection and window extraction.

Media is generated with ffmpeg and served from a local HTTP server with range support.
"""

from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import sys
import threading
import time
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path

import pytest

from cuesync import audio
from cuesync.audio import (
    SAMPLE_RATE,
    AudioError,
    AudioTrack,
    AudioWindow,
    extract_windows,
    probe,
    select_audio_track,
    window_starts,
)

# 0.5 s bursts of a 1 kHz tone at 0, 10, 20 s and so on.
BURSTS = "aevalsrc='if(lt(mod(t,10),0.5),sin(2*PI*1000*t),0)':s=48000:d={d}"
PEAK = 3000


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    root = tmp_path_factory.mktemp("media")
    long = root / "long.mkv"
    ffmpeg("-f", "lavfi", "-i", BURSTS.format(d=240), "-ac", "2", "-c:a", "pcm_s16le", str(long))
    short = root / "short.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        BURSTS.format(d=60),
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(short),
    )
    tracks = root / "tracks.mkv"
    ffmpeg(
        "-t",
        "30",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono",
        "-f",
        "lavfi",
        "-i",
        BURSTS.format(d=30),
        "-map",
        "0:a",
        "-map",
        "1:a",
        "-c:a",
        "pcm_s16le",
        "-metadata:s:a:0",
        "language=eng",
        "-metadata:s:a:1",
        "language=fra",
        "-disposition:a:0",
        "default",
        "-disposition:a:1",
        "0",
        str(tracks),
    )
    return {
        "/long.mkv": long.read_bytes(),
        "/short.mp4": short.read_bytes(),
        "/tracks.mkv": tracks.read_bytes(),
    }


class RangeServer:
    def __init__(self, files: dict[str, bytes], delay_s: float = 0.0) -> None:
        self.files = files
        self.delay_s = delay_s
        self.served: dict[str, int] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
                super().setup()

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                data = owner.files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                size = len(data)
                found = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if found:
                    first = int(found.group(1))
                    last = min(int(found.group(2) or size - 1), size - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
                else:
                    first, last = 0, size - 1
                    self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(last - first + 1))
                self.end_headers()
                try:
                    for pos in range(first, last + 1, 65536):
                        chunk = data[pos : min(pos + 65536, last + 1)]
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        owner.served[self.path] = owner.served.get(self.path, 0) + len(chunk)
                        if owner.delay_s:
                            time.sleep(owner.delay_s)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"


@pytest.fixture(scope="session")
def server(media: dict[str, bytes]):
    srv = RangeServer(media)
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture(scope="session")
def slow_server(media: dict[str, bytes]):
    srv = RangeServer(media, delay_s=0.05)  # about 1.3 MB/s
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


def first_loud(window: AudioWindow) -> float | None:
    pcm = array("h", window.samples)
    for i, sample in enumerate(pcm):
        if abs(sample) > PEAK:
            return i / SAMPLE_RATE
    return None


def peak(window: AudioWindow) -> int:
    return max((abs(s) for s in array("h", window.samples)), default=0)


@pytest.mark.parametrize(
    ("path", "duration"), [("/long.mkv", 240.0), ("/short.mp4", 60.0)], ids=["mkv", "mp4"]
)
def test_windows_timing_and_length(server: RangeServer, path: str, duration: float):
    url = server.url(path)

    async def go():
        info = await probe(url)
        windows = await extract_windows(url, info.duration_s, count=3, length_s=10, edge_s=12)
        return info, windows

    info, windows = asyncio.run(go())
    assert info.duration_s == pytest.approx(duration, abs=0.1)
    assert len(windows) == 3
    for window in windows:
        assert abs(len(window.samples) // 2 - 10 * SAMPLE_RATE) <= 800
        expected = (-window.start_s) % 10
        if expected >= 9.5:  # the window starts inside a burst
            expected = 0.0
        found = first_loud(window)
        assert found is not None
        assert found == pytest.approx(expected, abs=0.03)


def test_seeking_reads_part_of_the_file(server: RangeServer, media: dict[str, bytes]):
    server.served["/long.mkv"] = 0
    windows = asyncio.run(
        extract_windows(server.url("/long.mkv"), 240, count=1, length_s=5, edge_s=60)
    )
    assert len(windows) == 1
    assert server.served["/long.mkv"] < len(media["/long.mkv"]) * 0.25


def test_timed_out_window_keeps_the_audio_decoded_so_far(slow_server: RangeServer):
    windows = asyncio.run(
        extract_windows(
            slow_server.url("/long.mkv"), 240, count=1, length_s=60, edge_s=60, timeout_s=2.5
        )
    )
    assert len(windows) == 1
    window = windows[0]
    assert window.start_s == pytest.approx(90.0)
    assert 1 < window.duration_s < 60
    assert first_loud(window) == pytest.approx(0.0, abs=0.03)


def test_window_starts_spread():
    starts = window_starts(7200, 5, 120, 60)
    assert len(starts) == 5
    assert all(s >= 60 and s + 120 <= 7140 for s in starts)
    gaps = [b - a for a, b in pairwise(starts)]
    assert all(g == pytest.approx(gaps[0], abs=0.01) for g in gaps)
    assert all(g >= 120 for g in gaps)


def test_window_starts_short_file_drops_the_margin():
    assert window_starts(200, 5, 120) == [pytest.approx(40.0)]
    assert window_starts(30, 3, 120) == [0.0]


def test_window_starts_without_duration():
    assert window_starts(0, 5, 120) == []
    assert window_starts(100, 0, 10) == []


def track(index: int, language: str | None, default: bool = False) -> AudioTrack:
    return AudioTrack(index, language, default)


def test_select_track_matches_language_aliases():
    tracks = [track(0, "eng", True), track(1, "fra")]
    assert select_audio_track(tracks, "fre") == 1
    assert select_audio_track(tracks, "FRA") == 1
    assert select_audio_track([track(0, "eng", True), track(1, "zho")], "zht") == 1


def test_select_track_falls_back_to_default_then_first():
    assert select_audio_track([track(0, "eng"), track(1, "fra", True)], "jpn") == 1
    assert select_audio_track([track(0, "eng"), track(1, "fra")], "jpn") == 0
    assert select_audio_track([track(0, None, True), track(1, "fra")], None) == 0


def test_select_track_prefers_default_among_matches():
    tracks = [track(0, "fra"), track(1, "fre", True), track(2, "eng", True)]
    assert select_audio_track(tracks, "fre") == 1


def test_select_track_needs_a_track():
    with pytest.raises(AudioError):
        select_audio_track([], "eng")


def test_probe_and_extract_by_track(server: RangeServer):
    url = server.url("/tracks.mkv")

    async def go():
        info = await probe(url)
        index = select_audio_track(info.audio_tracks, "fre")
        french = await extract_windows(
            url, info.duration_s, count=1, length_s=15, edge_s=0, track=index
        )
        default = await extract_windows(url, info.duration_s, count=1, length_s=15, edge_s=0)
        return info, index, french, default

    info, index, french, default = asyncio.run(go())
    assert [(t.language, t.default) for t in info.audio_tracks] == [("eng", True), ("fra", False)]
    assert index == 1
    assert peak(french[0]) > PEAK
    assert peak(default[0]) < 100
    assert select_audio_track(info.audio_tracks, "jpn") == 0


class Recorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.procs: list[asyncio.subprocess.Process] = []
        real = asyncio.create_subprocess_exec

        async def create(*args, **kwargs):
            proc = await real(*args, **kwargs)
            self.procs.append(proc)
            return proc

        monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", create)


SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_timeout_kills_the_process(monkeypatch: pytest.MonkeyPatch):
    recorder = Recorder(monkeypatch)
    with pytest.raises(AudioError, match="timed out"):
        asyncio.run(audio._run(SLEEP, timeout=0.5))
    assert len(recorder.procs) == 1
    assert recorder.procs[0].returncode is not None


def test_cancellation_kills_the_process(monkeypatch: pytest.MonkeyPatch):
    recorder = Recorder(monkeypatch)

    async def go():
        task = asyncio.ensure_future(audio._run(SLEEP, timeout=30))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(recorder.procs) == 1
    assert recorder.procs[0].returncode is not None


def test_cancelled_extraction_leaves_no_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    recorder = Recorder(monkeypatch)
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(audio, "FFMPEG", str(fake))

    async def go():
        task = asyncio.ensure_future(extract_windows("http://x/", 1000, count=3, length_s=5))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(recorder.procs) == 3
    assert all(p.returncode is not None for p in recorder.procs)


def test_failure_does_not_leak_the_url(server: RangeServer, caplog: pytest.LogCaptureFixture):
    url = server.url("/missing.mkv?token=secret")
    with caplog.at_level("DEBUG"), pytest.raises(AudioError) as caught:
        asyncio.run(extract_windows(url, 600, count=2, length_s=5))
    assert "secret" not in str(caught.value)
    assert url not in str(caught.value)
    assert "secret" not in caplog.text
    with pytest.raises(AudioError) as caught:
        asyncio.run(probe(url))
    assert "secret" not in str(caught.value)


def fake_tool(tmp_path: Path, body: str) -> str:
    script = tmp_path / "tool"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_timed_out_window_keeps_whole_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    recorder = Recorder(monkeypatch)
    monkeypatch.setattr(audio, "FFMPEG", fake_tool(tmp_path, "printf abc\nexec sleep 30"))
    with caplog.at_level("DEBUG"):
        windows = asyncio.run(
            extract_windows("http://x/?token=secret", 1000, count=1, length_s=5, timeout_s=0.5)
        )
    assert len(windows) == 1
    assert windows[0].samples == b"ab"
    assert recorder.procs
    assert all(p.returncode is not None for p in recorder.procs)
    assert "secret" not in caplog.text


def test_timed_out_window_without_audio_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    recorder = Recorder(monkeypatch)
    monkeypatch.setattr(audio, "FFMPEG", fake_tool(tmp_path, "exec sleep 30"))
    with caplog.at_level("DEBUG"), pytest.raises(AudioError, match="no audio window") as caught:
        asyncio.run(
            extract_windows("http://x/?token=secret", 1000, count=2, length_s=5, timeout_s=0.5)
        )
    assert len(recorder.procs) == 2
    assert all(p.returncode is not None for p in recorder.procs)
    assert "secret" not in str(caught.value)
    assert "secret" not in caplog.text


def test_run_timeout_carries_the_output_so_far(monkeypatch: pytest.MonkeyPatch):
    recorder = Recorder(monkeypatch)
    with pytest.raises(audio.AudioTimeout) as caught:
        asyncio.run(audio._run(SLEEP, timeout=0.5))
    assert caught.value.output == b""
    assert recorder.procs[0].returncode is not None


def test_probe_fails_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(audio, "FFPROBE", fake_tool(tmp_path, "exec sleep 30"))
    with pytest.raises(AudioError, match="timed out"):
        asyncio.run(probe("http://x/?token=secret", timeout=0.5))


def test_concurrency_is_limited(monkeypatch: pytest.MonkeyPatch):
    running = peak_running = 0

    async def fake(args: list[str], timeout: float) -> bytes:
        nonlocal running, peak_running
        running += 1
        peak_running = max(peak_running, running)
        await asyncio.sleep(0.05)
        running -= 1
        return b"\x01\x00" * 100

    monkeypatch.setattr(audio, "_run", fake)
    windows = asyncio.run(
        extract_windows("http://x/", 3000, count=5, length_s=10, max_concurrent=2)
    )
    assert peak_running == 2
    assert len(windows) == 5
    assert [w.start_s for w in windows] == sorted(w.start_s for w in windows)


def test_failed_windows_are_dropped(monkeypatch: pytest.MonkeyPatch):
    async def fake(args: list[str], timeout: float) -> bytes:
        if float(args[args.index("-ss") + 1]) < 1000:
            raise AudioError("ffmpeg failed (exit 1)")
        return b"\x01\x00" * 100

    monkeypatch.setattr(audio, "_run", fake)
    windows = asyncio.run(extract_windows("http://x/", 3000, count=4, length_s=10))
    assert 0 < len(windows) < 4
    assert all(w.start_s >= 1000 for w in windows)


def test_missing_binary(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(AudioError, match="not installed"):
        asyncio.run(audio._run(["/nonexistent/ffprobe", "x"], timeout=1))


def test_window_duration():
    assert AudioWindow(0, b"\x00\x00" * SAMPLE_RATE).duration_s == 1.0


def test_probe_rejects_bad_output(monkeypatch: pytest.MonkeyPatch):
    async def fake(args: list[str], timeout: float) -> bytes:
        return b'{"format": {}, "streams": []}'

    monkeypatch.setattr(audio, "_run", fake)
    with pytest.raises(AudioError):
        asyncio.run(probe("http://x/"))
