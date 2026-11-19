"""Run the real configured pipeline against an already-running local API."""
import argparse
import time
from pathlib import Path
from secrets import token_hex

import httpx


def wait(client, job):
    deadline = time.monotonic() + 900
    previous = None
    while time.monotonic() < deadline:
        response = client.get("/jobs/" + job["id"])
        response.raise_for_status()
        current = response.json()
        message = f"{current['status']} · {current['stage']} · {current['progress']}%"
        if message != previous:
            print(message, flush=True)
            previous = message
        if current["status"] == "complete":
            return
        if current["status"] in ("cancelled", "failed"):
            raise RuntimeError(current["error"] or current["stage"])
        time.sleep(2)
    raise TimeoutError("Job did not finish within 15 minutes")


def checked(response):
    if response.is_error:
        raise RuntimeError(f"{response.status_code} {response.text}")
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", help="Local video path or an authorized direct HTTPS video URL")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, default=Path("storage/samples/smoke-export.mp4"))
    parser.add_argument("--email")
    args = parser.parse_args()
    with httpx.Client(base_url=args.api, timeout=75) as client:
        checked(client.post("/auth/register", json={
            "email": args.email or f"smoke-{token_hex(5)}@example.com", "password": token_hex(24)
        }))
        project = checked(client.post("/projects", json={
            "name": "ClipForge sample", "discovery": {"count": 3, "ranges": [[15, 30]], "language": "en"}
        }))
        base = "/projects/" + project["id"]
        print(f"Project {project['id']}", flush=True)
        if args.sample.startswith("https://"):
            job = checked(client.post(base + "/url", json={"url": args.sample, "authorized": True}))
        else:
            sample = Path(args.sample)
            with sample.open("rb") as video:
                job = checked(client.post(base + "/upload", data={"authorized": "true"},
                                          files={"file": (sample.name, video, "video/mp4")}))
        wait(client, job)
        transcript = checked(client.get(base + "/transcript"))
        assert transcript["segments"] and any(segment["words"] for segment in transcript["segments"])
        print(f"Transcribed {len(transcript['segments'])} segments in {transcript['language']}", flush=True)
        candidates = checked(client.get(base + "/candidates"))
        candidate = candidates[0]
        print(f"Selected {candidate['title']} ({candidate['start_time']:.2f}–{candidate['end_time']:.2f}s)", flush=True)
        clip = checked(client.post(base + "/clips", json={"candidate_id": candidate["id"]}))
        wait(client, checked(client.post(base + f"/clips/{clip['id']}/render", json=clip["settings"])))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        for extension in (".mp4", ".srt"):
            asset = clip["id"] + extension
            link = checked(client.get(base + f"/asset-links/{asset}?download=true"))["url"]
            destination = args.output.with_suffix(extension)
            with client.stream("GET", link, follow_redirects=True) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
            print(f"Downloaded {destination}", flush=True)


if __name__ == "__main__":
    main()
