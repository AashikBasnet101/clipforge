import logging
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

from celery import Celery
from sqlalchemy import delete, select, update

from .config import REDIS
from .db import Candidate, Clip, DB, Job, Project, Segment, Session, Source, Transcript, Usage, timestamp
from .schemas import ClipSettings, Discovery
from .services import ai, ingest, media
from . import storage


queue = Celery("clipforge", broker=REDIS)
queue.conf.update(
    task_ignore_result=True, task_acks_late=True, worker_prefetch_multiplier=1,
    task_soft_time_limit=3000, task_time_limit=3300,
    broker_transport_options={"visibility_timeout": 3600},
    beat_schedule={
        "dispatch-durable-jobs": {"task": "app.worker.dispatch_pending", "schedule": 15.0},
        "expire-temporary-files": {"task": "app.worker.expire_temporary", "schedule": 3600.0},
    },
)
log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


def stage(db, job, title, progress):
    result = db.execute(update(Job).where(Job.id == job.id, Job.status == "running")
                        .values(stage=title, progress=progress))
    if not result.rowcount:
        raise Cancelled()
    db.commit()


def segments_for(db, project_id):
    return list(db.scalars(select(Segment).join(Transcript).where(Transcript.project_id == project_id)
                           .order_by(Segment.start, Segment.id)))


def add_usage(db, project_id, metric, amount, model=None):
    db.add(Usage(project_id=project_id, metric=metric, amount=amount, model=model))


def analyze(db, job, project, source, folder):
    settings = Discovery.model_validate(job.snapshot or project.discovery)
    if source.kind == "url" and not source.size:
        stage(db, job, "Downloading approved video", 5)
        incoming = folder / "source.media"
        size = ingest.fetch(source.url, incoming)
        meta = media.probe(incoming)
        storage.store(incoming, source.key, meta["mime"])
        source.size = size
        source.url = None
        db.commit()
    with storage.materialize(source.key) as path:
        stage(db, job, "Inspecting video", 12)
        meta = media.probe(path)
        for field in ("duration", "width", "height", "mime"):
            setattr(source, field, meta[field])
        source.state = "validated"
        db.commit()
        if not source.preview_key and media.browser_compatible(meta):
            source.preview_key = source.key
            db.commit()
        if not source.preview_key:
            stage(db, job, "Encoding source preview", 18)
            preview = folder / "preview.mp4"
            media.preview(path, preview)
            source.preview_key = f"{project.id}/preview.mp4"
            storage.store(preview, source.preview_key, "video/mp4")
            add_usage(db, project.id, "stored_bytes", preview.stat().st_size)
            db.commit()
        segments = segments_for(db, project.id)
        if not segments:
            all_segments = []
            # 8-minute 16 kHz mono chunks remain under typical remote upload limits.
            for start in range(0, int(source.duration + .999), 480):
                duration = min(480, source.duration-start)
                if duration <= 0:
                    break
                stage(db, job, "Extracting audio and transcribing", 25 + int(32*start/source.duration))
                audio = folder / ("speech.wav" if os.getenv("STT_PROVIDER") == "local" else "speech.flac")
                media.extract(path, audio, start, duration)
                language, raw, model = ai.transcribe(audio, settings.language)
                all_segments.extend(ai.validated_segments(raw, start, duration))
                add_usage(db, project.id, "transcription_minutes", duration/60, model)
                db.commit()
            if not all_segments:
                raise ai.ProviderError("No speech was detected. Try a video with clearer spoken audio")
            transcript = Transcript(project_id=project.id, language=language, provider=model)
            db.add(transcript)
            db.flush()
            db.add_all(Segment(transcript_id=transcript.id, **item) for item in all_segments)
            db.commit()
            segments = segments_for(db, project.id)
    stage(db, job, "Finding standalone moments", 66)
    items = []
    batches = list(ai.batches(segments))
    for index, rows in enumerate(batches):
        stage(db, job, f"Ranking excerpts ({index+1}/{len(batches)})", 70 + 23 * index // len(batches))
        raw, usage, model = ai.suggest(rows, settings.ranges, settings.count)
        items.extend(raw)
        for key in ("prompt_tokens", "completion_tokens"):
            if usage.get(key):
                add_usage(db, project.id, key, usage[key], model)
        db.commit()
    candidates = ai.select(items, segments, source.duration, settings)
    if not candidates:
        raise ai.ProviderError("No complete excerpts matched the duration ranges. Adjust discovery settings and retry")
    stage(db, job, "Saving candidates", 95)
    saved = select(Clip.candidate_id).where(Clip.project_id == project.id)
    db.execute(delete(Candidate).where(Candidate.project_id == project.id, Candidate.id.not_in(saved)))
    db.add_all(Candidate(project_id=project.id, data=item.model_dump()) for item in candidates)
    source.state = "ready"


def render(db, job, source, folder):
    clip = db.get(Clip, job.clip_id)
    options = ClipSettings.model_validate(job.snapshot)
    stage(db, job, "Rendering video and captions", 25)
    with storage.materialize(source.key) as path:
        output, srt, mode = media.render(path, options, segments_for(db, job.project_id), folder,
                                         lambda: stage(db, job, "Validating MP4", 80))
    stage(db, job, "Saving export", 90)
    old_keys = (clip.key, clip.srt_key)
    output_key = f"{job.project_id}/{job.id}.mp4"
    srt_key = f"{job.project_id}/{job.id}.srt"
    try:
        storage.store(output, output_key, "video/mp4")
        storage.store(srt, srt_key, "application/x-subrip")
    except Exception:
        for key in (output_key, srt_key):
            try:
                storage.remove(key)
            except Exception:
                log.warning("Unable to remove incomplete export")
        raise
    clip.key, clip.srt_key, clip.framing = output_key, srt_key, mode
    clip.settings = job.snapshot
    clip.size = output.stat().st_size + srt.stat().st_size
    add_usage(db, job.project_id, "render_seconds", timestamp() - job.started_at)
    return old_keys


@queue.task
def process(job_id, attempt):
    with DB() as db:
        claim = db.execute(update(Job).where(Job.id == job_id, Job.attempt == attempt, Job.status == "queued")
                           .values(status="running", started_at=timestamp(), dispatched=True))
        db.commit()
        if not claim.rowcount:
            return
        job = db.get(Job, job_id)
        project = db.get(Project, job.project_id)
        source = db.scalar(select(Source).where(Source.project_id == project.id))
        try:
            with TemporaryDirectory(prefix="clipforge-job-") as temp:
                if job.kind == "analyze":
                    analyze(db, job, project, source, Path(temp))
                    old_keys = ()
                else:
                    old_keys = render(db, job, source, Path(temp))
                result = db.execute(update(Job).where(Job.id == job.id, Job.status == "running")
                                    .values(status="complete", stage="Complete", progress=100,
                                            seconds=timestamp()-job.started_at))
                if not result.rowcount:
                    db.rollback()
                    return
                db.commit()
            for key in old_keys:
                if key:
                    try:
                        storage.remove(key)
                    except Exception:
                        log.warning("Unable to remove previous export")
        except Cancelled:
            db.rollback()
        except Exception as exc:
            db.rollback()
            known = isinstance(exc, (ai.ProviderError, media.MediaError, ingest.IngestError))
            if not known:
                log.error("Unexpected job error %s (%s)", job_id, type(exc).__name__)
            db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(
                status="failed", stage="Failed", error=str(exc) if known else "Processing failed. Check worker logs and retry",
                seconds=timestamp()-job.started_at))
            db.commit()


def dispatch(job_id):
    with DB() as db:
        job = db.get(Job, job_id)
        if job and job.status == "queued" and not job.dispatched:
            try:
                process.apply_async(args=[job.id, job.attempt], retry=False)
            except Exception:
                log.warning("Job %s awaits broker recovery", job_id)
                return
            db.execute(update(Job).where(Job.id == job.id, Job.attempt == job.attempt).values(dispatched=True))
            db.commit()


@queue.task
def dispatch_pending():
    with DB() as db:
        db.execute(update(Job).where(Job.status == "running", Job.started_at < timestamp()-3400)
                   .values(status="failed", stage="Interrupted", error="Worker timed out or restarted. Retry this job"))
        db.execute(update(Job).where(Job.status == "queued", Job.dispatched, Job.updated_at < timestamp()-3600)
                   .values(dispatched=False, attempt=Job.attempt+1))
        ids = list(db.scalars(select(Job.id).where(Job.status == "queued", Job.dispatched.is_(False)).limit(100)))
        db.commit()
    for job_id in ids:
        dispatch(job_id)


@queue.task
def expire_temporary():
    cutoff = timestamp() - 3 * 3600
    folder = Path(gettempdir())
    for pattern in ("clipforge-job-*", "clipforge-upload-*"):
        for path in folder.glob(pattern):
            try:
                info = path.lstat()
                if path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid() and info.st_mtime < cutoff:
                    shutil.rmtree(path)
            except OSError:
                log.warning("Could not remove an expired temporary directory")
    with DB() as db:
        db.execute(delete(Session).where(Session.expires < timestamp()))
        db.commit()
