import os
import shutil
import socket
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ["APP_SECRET"] = "test-secret-only-" * 3
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["MEDIA_URL_HOSTS"] = "media.example.com"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, storage, worker
from app.db import Base, Job
from app.schemas import CandidateData, ClipSettings, Discovery
from app.services import ai, ingest, media


@pytest.fixture
def services(monkeypatch, tmp_path):
    database = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(database)
    factory = sessionmaker(database, expire_on_commit=False)
    for module in (main, worker):
        monkeypatch.setattr(module, "DB", factory)
    monkeypatch.setattr(storage, "STORAGE", tmp_path / "media")
    monkeypatch.setattr(main, "rate", lambda *args: None)
    monkeypatch.setattr(main, "dispatch", lambda _: None)
    with TestClient(main.app) as client:
        yield client, factory
    database.dispose()


def signup(client, name="creator"):
    response = client.post("/auth/register", json={"email": f"{name}@example.com", "password": "account passphrase 2026"})
    assert response.status_code == 201, response.text


def project(client):
    response = client.post("/projects", json={"name": "Interview", "discovery": {"ranges": [[3, 8]], "count": 3}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def candidate(start=0, end=6, score=80):
    return {"start_time": start, "end_time": end, "title": "One useful idea", "hook": "Start here",
            "summary": "A complete point", "category": "educational", "reasoning_summary": "Clear payoff",
            "scores": {"hook_strength": score, "clarity": score, "emotional_impact": score,
                       "standalone_value": score, "overall": score}}


def test_authentication_and_ownership(services):
    client, _ = services
    assert client.get("/projects").status_code == 401
    signup(client)
    key = project(client)
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"email": "creator@example.com", "password": "wrong password here"}).status_code == 401
    assert client.post("/auth/login", json={"email": "creator@example.com", "password": "account passphrase 2026"}).status_code == 200
    with TestClient(main.app) as other:
        signup(other, "other")
        assert other.get(f"/projects/{key}").status_code == 404
        assert other.get(f"/projects/{key}/asset-links/source").status_code == 404
        assert other.post(f"/projects/{key}/upload", files={"file": ("demo.mp4", b"fake")}, data={"authorized": "true"}).status_code == 404


def test_upload_rejection(services):
    client, _ = services
    signup(client)
    key = project(client)
    path = f"/projects/{key}/upload"
    assert client.post(path, files={"file": ("fake.mp4", b"bad data")}, data={"authorized": "true"}).status_code == 415
    assert client.post(path, files={"file": ("fake.mp4", b"bad data")}, data={"authorized": "false"}).status_code == 422
    assert client.get(f"/projects/{key}").json()["source"] is None


def test_url_security(monkeypatch):
    def lookup(address):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))]
    monkeypatch.setattr(ingest.socket, "getaddrinfo", lambda *a, **k: lookup("127.0.0.1"))
    with pytest.raises(ingest.IngestError):
        ingest.resolve("https://media.example.com/video.mp4")
    monkeypatch.setattr(ingest.socket, "getaddrinfo", lambda *a, **k: lookup("93.184.216.34"))
    assert ingest.resolve("https://media.example.com/video.mp4")[1] == "93.184.216.34"
    for url in ["http://media.example.com/x", "https://media.example.com.evil.net/x",
                "https://media.example.com:8443/x", "https://user@media.example.com/x"]:
        with pytest.raises(ingest.IngestError):
            ingest.resolve(url)
    class Redirect:
        def __init__(self, host, ip):
            assert ip == "93.184.216.34"
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return SimpleNamespace(status=302)
        def close(self):
            pass
    monkeypatch.setattr(ingest, "PinnedHTTPS", Redirect)
    with pytest.raises(ingest.IngestError, match="Redirects"):
        ingest.fetch("https://media.example.com/video.mp4", None)


def test_public_url_mode_keeps_network_and_platform_checks(monkeypatch):
    monkeypatch.delenv("MEDIA_URL_HOSTS")
    monkeypatch.setenv("ALLOW_PUBLIC_MEDIA_URLS", "1")
    monkeypatch.setattr(ingest.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443)),
    ])
    parts, address = ingest.resolve("https://media.example.com/film.mp4?signature=example")
    assert parts.query == "signature=example" and address == "93.184.216.34"
    with pytest.raises(ingest.IngestError, match="official export"):
        ingest.resolve("https://www.youtube.com/watch?v=video")
    assert not ingest.public_address("::ffff:127.0.0.1")
    assert not ingest.public_address("169.254.169.254")
    monkeypatch.delenv("ALLOW_PUBLIC_MEDIA_URLS")
    with pytest.raises(ingest.IngestError, match="disabled"):
        ingest.resolve("https://media.example.com/film.mp4")


def test_timestamp_validation_and_deduplication():
    raw = [{"start": -.1, "end": 20, "text": "This is one complete example sentence.",
            "words": [{"word": "This", "start": -.4, "end": .35},
                      {"word": "outside", "start": 9, "end": 10}]}]
    rows = [SimpleNamespace(**row) for row in ai.validated_segments(raw, 12, 6)]
    assert [(row.start, row.end) for row in rows] == [(12, 18)]
    assert rows[0].words[0]["start"] == 12
    assert len(rows[0].words) == 1
    with pytest.raises(ValueError):
        CandidateData.model_validate(candidate(score=101))
    result = ai.select([candidate(12.1,18,60), candidate(12,18,85), candidate(12,42), candidate(14,18)],
                       rows, 18, Discovery(ranges=[(3,8)]))
    assert [(item.start_time, item.end_time, item.scores.overall) for item in result] == [(12,18,85)]


def test_subtitle_generation(tmp_path):
    options = ClipSettings(start=2, end=6, title="Test", style="highlight", overlay="Brand")
    segment = SimpleNamespace(start=2, end=4, text="नमस्ते world",
                              words=[{"word": "नमस्ते", "start": 2, "end": 2.8},
                                     {"word": "world", "start": 3, "end": 4}])
    ass, srt = media.captions_files([segment], options, 1080, 1920, tmp_path)
    assert "00:00:00,000 --> 00:00:02,000" in srt.read_text()
    assert "नमस्ते world" in srt.read_text()
    assert "Brand" in ass.read_text() and r"{\c&H00" in ass.read_text()


def test_upload_transcribe_retry_render_and_download(services, monkeypatch, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.fail("Install FFmpeg with libass to run the video workflow test")
    client, factory = services
    signup(client)
    key = project(client)
    base = f"/projects/{key}"
    input_video = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=navy:s=320x180:r=24",
                    "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=16000",
                    "-t", "7", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(input_video)], check=True)
    words = [{"word": word, "start": i*.7, "end": i*.7+.65}
             for i, word in enumerate("This is an example of a complete thought".split())]
    calls = []
    def transcribe(path, language):
        assert path.is_file()
        calls.append(language)
        return "en", [{"start": 0, "end": 6, "text": "This is an example of a complete thought", "words": words}], "test-stt"
    def unavailable(*args):
        raise ai.ProviderError("Test LLM unavailable")
    monkeypatch.setattr(ai, "transcribe", transcribe)
    monkeypatch.setattr(ai, "suggest", unavailable)
    response = client.post(base + "/upload", files={"file": ("../../sample.mp4", input_video.read_bytes(), "video/mp4")},
                           data={"authorized": "true"})
    assert response.status_code == 202, response.text
    job = response.json()
    worker.process(job["id"], job["attempt"])
    assert client.get(f"/jobs/{job['id']}").json()["status"] == "failed"
    assert "unavailable" in client.get(f"/jobs/{job['id']}").json()["error"]
    assert client.get(base + "/transcript").json()["segments"][0]["words"][0]["start"] == 0
    assert not storage.local_path(f"{key}/preview.mp4").exists()
    source_link = client.get(base + "/asset-links/source").json()["url"]
    assert client.get(source_link, headers={"Range": "bytes=0-99"}).status_code == 206
    monkeypatch.setattr(ai, "suggest", lambda *args: ([candidate()], {"prompt_tokens": 42}, "test-llm"))
    retry = client.post(f"/jobs/{job['id']}/retry").json()
    worker.process(retry["id"], retry["attempt"])
    worker.process(retry["id"], retry["attempt"])
    assert calls == ["auto"]
    assert client.get(f"/jobs/{retry['id']}").json()["status"] == "complete"
    found = client.get(base + "/candidates").json()
    assert len(found) == 1
    clip = client.post(base + "/clips", json={"candidate_id": found[0]["id"]}).json()
    assert client.post(base + f"/clips/{clip['id']}/render", json={**clip["settings"], "end": 8}).status_code == 422
    render = client.post(base + f"/clips/{clip['id']}/render", json=clip["settings"]).json()
    worker.process(render["id"], render["attempt"])
    status = client.get(f"/jobs/{render['id']}").json()
    assert status["status"] == "complete", status
    link = client.get(base + f"/asset-links/{clip['id']}.mp4?download=true").json()["url"]
    with TestClient(main.app) as other:
        signup(other, "outsider")
        assert other.get(link).status_code == 404
    tampered = re.sub(r"sig=[0-9a-f]+", "sig=" + "0" * 64, link)
    assert client.get(tampered).status_code == 403
    expired = re.sub(r"expires=\d+", "expires=1", link)
    assert client.get(expired).status_code == 403
    download = client.get(link)
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    output = tmp_path / "export.mp4"
    output.write_bytes(download.content)
    meta = media.probe(output, source=False)
    assert (meta["width"], meta["height"], meta["video_codec"], meta["audio_codec"]) == (1080,1920,"h264","aac")
    assert client.get(link, headers={"Range": "bytes=0-99"}).status_code == 206
    srt = client.get(base + f"/asset-links/{clip['id']}.srt").json()["url"]
    assert "This is an example of" in client.get(srt).text
    args = ["ffmpeg", "-v", "error", "-ss", "1", "-i", str(output), "-frames:v", "1",
            "-vf", "crop=1080:300:0:1380", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    pixels = subprocess.check_output(args)
    assert any(min(pixels[i:i+3]) > 200 for i in range(0, len(pixels), 3)), "Expected visible white caption pixels"
    with factory() as db:
        assert db.get(Job, render["id"]).progress == 100


def test_cancelled_job_cannot_be_claimed(services):
    client, _ = services
    signup(client)
    key = project(client)
    response = client.post(f"/projects/{key}/upload", files={"file": ("sample.mp4", b"0000ftypisom")},
                           data={"authorized": "true"})
    assert response.status_code == 202
    job = response.json()
    assert client.post(f"/jobs/{job['id']}/cancel").status_code == 200
    worker.process(job["id"], job["attempt"])
    assert client.get(f"/jobs/{job['id']}").json()["status"] == "cancelled"


def test_discovery_windows_avoid_obvious_mid_sentence_cuts():
    rows = [(0, 5, "Ask a question that gets your guest to tell"),
            (5, 10, "the story."), (10, 15, "The viewer gets a complete idea.")]
    windows = ai.eligible_windows(rows, [(3, 15)]).values()
    assert (0, 1) in windows
    assert (0, 0) not in windows
    assert all(start != 1 for start, _ in windows)


def test_compact_provider_output_can_reuse_generated_title(monkeypatch):
    import json
    from unittest.mock import MagicMock

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.post.return_value.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"candidates": [{
            "window_id": "W1", "hook": "Start with the viewer's question",
            "summary": "An actionable example", "youtube": "How to Open a Video",
            "scores": [90, 85, 65, 95],
        }]})}}],
    }
    monkeypatch.setattr(ai, "provider", lambda _: connection)
    found, _, _ = ai.suggest([(0, 18, "Here is a complete standalone excerpt.")], [(15, 30)], 3)
    item = CandidateData.model_validate(found[0])
    assert item.title == "How to Open a Video"
    assert set(item.platforms) == {"youtube"}
    assert (item.start_time, item.end_time) == (0, 18)
    assert item.scores.hook_strength == 90


def test_export_shortens_verified_silence_and_retimes_captions(tmp_path):
    source = tmp_path / "source.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24",
               "-f", "lavfi", "-i", "aevalsrc=if(between(t\\,2\\,4)\\,0\\,0.15*sin(880*PI*t)):s=48000",
               "-t", "6", "-c:v", "libx264", "-c:a", "aac", str(source)])
    segments = [
        SimpleNamespace(start=.3, end=2, text="An opening sentence", words=[]),
        SimpleNamespace(start=4, end=5.5, text="Return after the pause", words=[]),
    ]
    options = ClipSettings(start=0, end=6, title="Pause cut", shorten_pauses=True, framing="fit", fps=0)
    output, srt, framing = media.render(source, options, segments, tmp_path)
    meta = media.probe(output, source=False)
    assert meta["duration"] == pytest.approx(4.4, abs=.13)
    assert meta["frame_rate"] == "24/1"
    assert "1 pause shortened" in framing
    assert "00:00:02,400 --> 00:00:03,900" in srt.read_text()
