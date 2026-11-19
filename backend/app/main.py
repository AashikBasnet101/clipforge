import hashlib
import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import config, storage
from .db import Audit, Candidate, Clip, DB, Job, Project, Segment, Source, Session, Transcript, Usage, User, timestamp, uid
from .schemas import (
    AccountView, AssetLink, CandidateView, Caption, ClipDraft, ClipSettings, Credentials, Discovery,
    JobView, NewProject, ProjectDetail, ProjectView, SegmentEdit, SegmentView, SelectCandidate, SourceURL, TranscriptView,
)
from .services.ai import local_discovery
from .services.ingest import IngestError, resolve, url_mode
from .services.media import MediaError, cues, input_format
from .worker import dispatch, segments_for


@asynccontextmanager
async def lifespan(_):
    if len(config.SECRET) < 32:
        raise RuntimeError("Set APP_SECRET to at least 32 characters")
    yield


app = FastAPI(title="ClipForge AI", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[config.ORIGIN], allow_credentials=True,
                   allow_methods=["GET", "POST", "PATCH", "OPTIONS"], allow_headers=["Content-Type", "Range"])
hasher = PasswordHasher()
dummy_hash = hasher.hash(token_urlsafe())
redis = Redis.from_url(config.REDIS, socket_connect_timeout=2, socket_timeout=2)
cookie = "clipforge_session"


@app.exception_handler(RequestValidationError)
async def request_validation(_, exception):
    return JSONResponse({"detail": [{field: error[field] for field in ("loc", "msg", "type")}
                                   for error in exception.errors()]}, status_code=422)


@app.middleware("http")
async def guard(request, call_next):
    if request.method in ("POST", "PATCH", "DELETE", "PUT"):
        own_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("origin") not in (None, config.ORIGIN, own_origin):
            return JSONResponse({"detail": "Origin is not permitted"}, 403)
    if request.url.path.endswith("/upload") and request.method == "POST":
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, 400)
        if not size or size > config.MAX_BYTES + 64 * 1024:
            return JSONResponse({"detail": "Supply a file within the configured upload limit"}, 413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers.setdefault("Cache-Control", "private, no-store")
    return response


def database():
    with DB() as db:
        yield db


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current(request: Request, db=Depends(database)):
    value = request.cookies.get(cookie)
    session = db.get(Session, digest(value)) if value else None
    if not session or session.expires <= timestamp():
        raise HTTPException(401, "Sign in to continue")
    return db.get(User, session.owner_id)


def rate(request, name, limit, period, identity=None):
    address = identity or (request.client.host if request.client else "unknown")
    key = f"clipforge:rate:{name}:{digest(address)}:{int(timestamp()) // period}"
    try:
        with redis.pipeline() as pipe:
            count, _ = pipe.incr(key).expire(key, period * 2).execute()
    except RedisError:
        raise HTTPException(503, "Rate limiter is temporarily unavailable") from None
    if count > limit:
        raise HTTPException(429, "Too many requests. Try again later")


def sign_in(db, user, response, request):
    old = request.cookies.get(cookie)
    if old:
        session = db.get(Session, digest(old))
        if session:
            db.delete(session)
    token = token_urlsafe(32)
    db.add(Session(digest=digest(token), owner_id=user.id, expires=timestamp() + config.SESSION_SECONDS))
    db.add(Audit(owner_id=user.id, action="login"))
    db.commit()
    response.set_cookie(cookie, token, max_age=config.SESSION_SECONDS, httponly=True, secure=config.SECURE, samesite="lax")
    return {"email": user.email, "admin": user.admin}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", status_code=201, response_model=AccountView)
def register(data: Credentials, request: Request, response: Response, db=Depends(database)):
    if os.getenv("ALLOW_REGISTRATION", "1") != "1":
        raise HTTPException(403, "Registration is closed")
    rate(request, "register", 8, 900)
    user = User(email=str(data.email).lower(), password_hash=hasher.hash(data.password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(409, "This email is already registered") from None
    db.add(Audit(owner_id=user.id, action="register"))
    return sign_in(db, user, response, request)


@app.post("/auth/login", response_model=AccountView)
def login(data: Credentials, request: Request, response: Response, db=Depends(database)):
    email = str(data.email).lower()
    rate(request, "login", 25, 900)
    rate(request, "login-email", 10, 900, email)
    user = db.scalar(select(User).where(User.email == email))
    try:
        hasher.verify(user.password_hash if user else dummy_hash, data.password)
    except VerificationError:
        raise HTTPException(401, "Invalid email or password") from None
    if not user:
        raise HTTPException(401, "Invalid email or password")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(data.password)
    return sign_in(db, user, response, request)


@app.get("/auth/me", response_model=AccountView)
def me(user=Depends(current)):
    return {"email": user.email, "admin": user.admin}


@app.post("/auth/logout")
def logout(request: Request, response: Response, db=Depends(database)):
    value = request.cookies.get(cookie)
    session = db.get(Session, digest(value)) if value else None
    if session:
        db.delete(session)
        db.commit()
    response.delete_cookie(cookie, httponly=True, secure=config.SECURE, samesite="lax")
    return {"ok": True}


def owned(db, user, project_id):
    project = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == user.id))
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def entry(db, model, project_id, entry_id=None):
    query = select(model).where(model.project_id == project_id)
    if entry_id:
        query = query.where(model.id == entry_id)
    item = db.scalar(query)
    if not item:
        raise HTTPException(404, "Resource not found")
    return item


def reserve(db, user):
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    jobs = select(func.count()).select_from(Job).join(Project).where(Project.owner_id == user.id)
    if db.scalar(jobs.where(Job.status.in_(("queued", "running")))) >= config.MAX_ACTIVE:
        raise HTTPException(429, "Concurrent job limit reached")
    if db.scalar(jobs.where(Job.created_at >= timestamp() - 86400)) >= config.MAX_DAILY:
        raise HTTPException(429, "Daily processing limit reached")


def pack(item, fields):
    return {field: getattr(item, field) for field in fields.split()}


def job_view(job):
    return pack(job, "id project_id clip_id kind status stage progress error attempt seconds created_at updated_at")


def project_view(db, project, detail=False):
    source = db.scalar(select(Source).where(Source.project_id == project.id))
    jobs = list(db.scalars(select(Job).where(Job.project_id == project.id).order_by(Job.created_at.desc())))
    clips = list(db.scalars(select(Clip).where(Clip.project_id == project.id).order_by(Clip.created_at.desc())))
    result = {**pack(project, "id name discovery created_at"), "source": None, "job": job_view(jobs[0]) if jobs else None,
              "clip_count": sum(bool(c.key) for c in clips)}
    if source:
        result["source"] = {**pack(source, "filename kind size duration width height state"), "preview": bool(source.preview_key)}
    if detail:
        result["jobs"] = [job_view(job) for job in jobs]
        result["candidates"] = sorted(({"id": c.id, **c.data} for c in db.scalars(select(Candidate)
                                      .where(Candidate.project_id == project.id))),
                                      key=lambda c: c["scores"]["overall"], reverse=True)
        result["clips"] = [{**pack(c, "id candidate_id settings framing"), "available": bool(c.key)} for c in clips]
    return result


@app.post("/projects", status_code=201, response_model=ProjectView)
def create_project(data: NewProject, request: Request, user=Depends(current), db=Depends(database)):
    rate(request, "project", 30, 3600, user.id)
    if data.discovery.count > config.MAX_CLIPS:
        raise HTTPException(422, "Candidate limit exceeded")
    project = Project(owner_id=user.id, name=data.name.strip(), discovery=data.discovery.model_dump())
    db.add(project)
    db.commit()
    return project_view(db, project)


@app.get("/projects", response_model=list[ProjectView])
def projects(user=Depends(current), db=Depends(database)):
    query = select(Project).where(Project.owner_id == user.id).order_by(Project.created_at.desc())
    return [project_view(db, project) for project in db.scalars(query)]


@app.get("/projects/{project_id}", response_model=ProjectDetail)
def project(project_id: str, user=Depends(current), db=Depends(database)):
    return project_view(db, owned(db, user, project_id), True)


@app.patch("/projects/{project_id}/discovery", response_model=Discovery)
def discovery(project_id: str, data: Discovery, user=Depends(current), db=Depends(database)):
    item = owned(db, user, project_id)
    if data.count > config.MAX_CLIPS:
        raise HTTPException(422, "Candidate limit exceeded")
    item.discovery = data.model_dump()
    db.commit()
    return item.discovery


def new_source(db, user, project_id):
    project = owned(db, user, project_id)
    reserve(db, user)
    if db.scalar(select(Source.id).where(Source.project_id == project.id)):
        raise HTTPException(409, "This project already has a source")
    return project


def schedule(db, background, project_id, kind, snapshot=None, clip_id=None):
    job = Job(id=uid(), project_id=project_id, kind=kind, snapshot=snapshot, clip_id=clip_id)
    db.add(job)
    db.commit()
    background.add_task(dispatch, job.id)
    return job_view(job)


def safe_name(name):
    return re.sub(r"[^\w. ()-]", "_", Path(name.replace("\\", "/")).name)[:180] or "video"


@app.post("/projects/{project_id}/upload", status_code=202, response_model=JobView)
def upload(project_id: str, file: UploadFile, background: BackgroundTasks, authorized: bool = Form(),
           user=Depends(current), db=Depends(database)):
    if not authorized:
        raise HTTPException(422, "Confirm that you are entitled to process this video")
    project = new_source(db, user, project_id)
    with TemporaryDirectory(prefix="clipforge-upload-") as temp:
        incoming = Path(temp) / "incoming"
        size = 0
        with incoming.open("wb") as handle:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_BYTES:
                    raise HTTPException(413, "File exceeds the upload limit")
                handle.write(chunk)
        try:
            _, mime = input_format(incoming)
        except MediaError as exc:
            raise HTTPException(415, str(exc)) from None
        key = f"{project.id}/source.media"
        storage.store(incoming, key, mime)
    db.add(Source(project_id=project.id, filename=safe_name(file.filename or "video"), kind="upload", key=key, size=size))
    return schedule(db, background, project.id, "analyze", project.discovery)


@app.post("/projects/{project_id}/url", status_code=202, response_model=JobView)
def ingest_url(project_id: str, data: SourceURL, background: BackgroundTasks, user=Depends(current), db=Depends(database)):
    project = new_source(db, user, project_id)
    try:
        parts, _ = resolve(data.url)
    except IngestError as exc:
        raise HTTPException(422, str(exc)) from None
    db.add(Source(project_id=project.id, filename=safe_name(unquote(Path(parts.path).name)), kind="url",
                  key=f"{project.id}/source.media", url=data.url))
    return schedule(db, background, project.id, "analyze", project.discovery)


@app.get("/projects/{project_id}/transcript", response_model=TranscriptView)
def transcript(project_id: str, user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    item = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
    return {"language": item.language if item else None, "provider": item.provider if item else None,
            "segments": [pack(s, "id start end text words") for s in segments_for(db, project_id)]}


@app.patch("/projects/{project_id}/transcript/{segment_id}", response_model=SegmentView)
def edit_transcript(project_id: str, segment_id: str, data: SegmentEdit, user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    row = db.scalar(select(Segment).join(Transcript).where(Segment.id == segment_id, Transcript.project_id == project_id))
    if not row:
        raise HTTPException(404, "Segment not found")
    source = entry(db, Source, project_id)
    if data.end > source.duration or any(not data.start <= w.start < w.end <= data.end for w in data.words or []):
        raise HTTPException(422, "Transcript timing must be within the video and segment")
    row.start, row.end, row.text = data.start, data.end, data.text
    row.words = [word.model_dump() for word in data.words] if data.words else []
    db.commit()
    return pack(row, "id start end text words")


def owned_job(db, user, job_id):
    job = db.scalar(select(Job).join(Project).where(Job.id == job_id, Project.owner_id == user.id).with_for_update(of=Job))
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/jobs/{job_id}", response_model=JobView)
def read_job(job_id: str, user=Depends(current), db=Depends(database)):
    return job_view(owned_job(db, user, job_id))


@app.post("/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str, user=Depends(current), db=Depends(database)):
    job = owned_job(db, user, job_id)
    if job.status not in ("running", "queued"):
        raise HTTPException(409, "This job is already finished")
    job.status, job.stage = "cancelled", "Cancelled"
    db.commit()
    return job_view(job)


@app.post("/jobs/{job_id}/retry", status_code=202, response_model=JobView)
def retry_job(job_id: str, background: BackgroundTasks, user=Depends(current), db=Depends(database)):
    reserve(db, user)
    job = owned_job(db, user, job_id)
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(409, "Only failed or cancelled jobs can be retried")
    active = select(Job.id).where(Job.project_id == job.project_id, Job.kind == job.kind,
                                 Job.clip_id == job.clip_id, Job.status.in_(("queued", "running")))
    if db.scalar(active):
        raise HTTPException(409, "This resource is already processing")
    latest = db.scalar(select(Job.id).where(Job.project_id == job.project_id, Job.kind == job.kind,
                                           Job.clip_id == job.clip_id).order_by(Job.created_at.desc()).limit(1))
    if latest != job.id:
        raise HTTPException(409, "Retry the latest attempt for this resource")
    settings = db.get(Project, job.project_id).discovery if job.kind == "analyze" else job.snapshot
    return schedule(db, background, job.project_id, job.kind, settings, job.clip_id)


@app.post("/projects/{project_id}/analysis", status_code=202, response_model=JobView)
def analyze_again(project_id: str, background: BackgroundTasks, user=Depends(current), db=Depends(database)):
    project = owned(db, user, project_id)
    reserve(db, user)
    entry(db, Source, project_id)
    if not db.scalar(select(Transcript.id).where(Transcript.project_id == project_id)):
        raise HTTPException(409, "A saved transcript is required")
    if db.scalar(select(Job.id).where(Job.project_id == project_id, Job.kind == "analyze",
                                      Job.status.in_(("queued", "running")))):
        raise HTTPException(409, "The project is already being analyzed")
    return schedule(db, background, project_id, "analyze", project.discovery)


@app.get("/projects/{project_id}/candidates", response_model=list[CandidateView])
def candidates(project_id: str, user=Depends(current), db=Depends(database)):
    return project_view(db, owned(db, user, project_id), True)["candidates"]


@app.post("/projects/{project_id}/clips", status_code=201, response_model=ClipDraft)
def select_clip(project_id: str, data: SelectCandidate, user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    candidate = entry(db, Candidate, project_id, data.candidate_id)
    existing = db.scalar(select(Clip).where(Clip.candidate_id == candidate.id))
    if existing:
        return pack(existing, "id settings")
    if db.scalar(select(func.count()).select_from(Clip).where(Clip.project_id == project_id)) >= config.MAX_CLIPS:
        raise HTTPException(429, "Project clip limit reached")
    copy = candidate.data
    options = ClipSettings(start=copy["start_time"], end=copy["end_time"], title=copy["title"],
                           framing="smart", fps=0, shorten_pauses=True, normalize_audio=True,
                           hashtags=" ".join(copy.get("platforms", {}).get("youtube", {}).get("hashtags", []))[:500])
    clip = Clip(project_id=project_id, candidate_id=candidate.id, settings=options.model_dump())
    db.add(clip)
    db.commit()
    return pack(clip, "id settings")


@app.get("/projects/{project_id}/clips/{clip_id}/captions", response_model=list[Caption])
def clip_captions(project_id: str, clip_id: str, start: float | None = None, end: float | None = None,
                  words_per_line: int | None = None, user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    clip = entry(db, Clip, project_id, clip_id)
    options = ClipSettings.model_validate(clip.settings)
    if start is not None or end is not None or words_per_line is not None:
        try:
            options = ClipSettings.model_validate({**clip.settings, "start": options.start if start is None else start,
                        "end": options.end if end is None else end, "captions": None,
                        "words_per_line": options.words_per_line if words_per_line is None else words_per_line})
        except ValueError:
            raise HTTPException(422, "Invalid caption timing or line size") from None
    return [cue.model_dump() for cue, _ in cues(segments_for(db, project_id), options)]


@app.post("/projects/{project_id}/clips/{clip_id}/render", status_code=202, response_model=JobView)
def render_clip(project_id: str, clip_id: str, data: ClipSettings, background: BackgroundTasks,
                user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    reserve(db, user)
    entry(db, Clip, project_id, clip_id)
    source = entry(db, Source, project_id)
    if data.end > source.duration or any(c.end > data.end-data.start + .001 for c in data.captions or []):
        raise HTTPException(422, "Trim and captions must fit within the source")
    if db.scalar(select(Job.id).where(Job.clip_id == clip_id, Job.status.in_(("queued", "running")))):
        raise HTTPException(409, "This clip is already rendering")
    return schedule(db, background, project_id, "render", data.model_dump(), clip_id)


def asset_key(db, project_id, name):
    if name == "source":
        return entry(db, Source, project_id).preview_key, "video/mp4", "source.mp4"
    if "." not in name:
        raise HTTPException(404, "Asset not found")
    clip_id, suffix = name.rsplit(".", 1)
    clip = entry(db, Clip, project_id, clip_id)
    filename = re.sub(r"[^a-zA-Z0-9_-]+", "-", clip.settings["title"]).strip("-")[:100] or "clip"
    if suffix == "mp4":
        return clip.key, "video/mp4", filename + ".mp4"
    if suffix == "srt":
        return clip.srt_key, "application/x-subrip", filename + ".srt"
    raise HTTPException(404, "Asset not found")


def signature(owner, project, asset, expires, download, key):
    data = f"{owner}/{project}/{asset}/{expires}/{int(download)}/{key}"
    return hmac.new(config.SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()


@app.get("/projects/{project_id}/asset-links/{name}", response_model=AssetLink)
def asset_link(project_id: str, name: str, request: Request, download: bool = False,
               user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    rate(request, "asset-link", 120, 60, user.id)
    key, _, _ = asset_key(db, project_id, name)
    if not key:
        raise HTTPException(404, "Asset is not ready")
    expiration = int(timestamp()) + 900
    token = signature(user.id, project_id, name, expiration, download, key)
    return {"url": f"/projects/{project_id}/assets/{name}?expires={expiration}&sig={token}&download={int(download)}",
            "expires": expiration}


@app.get("/projects/{project_id}/assets/{name}")
def asset(project_id: str, name: str, expires: int, sig: str, download: bool = False,
          user=Depends(current), db=Depends(database)):
    owned(db, user, project_id)
    key, mime, filename = asset_key(db, project_id, name)
    if not key or expires < timestamp() or not hmac.compare_digest(sig, signature(user.id, project_id, name, expires, download, key)):
        raise HTTPException(403, "Asset link expired or invalid")
    if storage.bucket():
        params = {"Bucket": storage.bucket(), "Key": key}
        if download:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        response = RedirectResponse(storage.client().generate_presigned_url("get_object", Params=params, ExpiresIn=180), 307)
    else:
        path = storage.local_path(key)
        if not path.is_file():
            raise HTTPException(404, "Asset unavailable")
        response = FileResponse(path, media_type=mime, filename=filename if download else None)
    if download:
        db.add(Usage(project_id=project_id, metric="download_requests", amount=1))
        db.commit()
    return response


@app.get("/usage")
def usage(user=Depends(current), db=Depends(database)):
    owned_ids = select(Project.id).where(Project.owner_id == user.id)
    jobs = list(db.scalars(select(Job).where(Job.project_id.in_(owned_ids))))
    completed = [job for job in jobs if job.status == "complete"]
    records = list(db.scalars(select(Usage).where(Usage.project_id.in_(owned_ids))))
    totals = {}
    models = {}
    for record in records:
        totals[record.metric] = totals.get(record.metric, 0) + record.amount
        if record.model:
            item = models.setdefault(record.model, {})
            item[record.metric] = item.get(record.metric, 0) + record.amount
    storage_bytes = (db.scalar(select(func.sum(Source.size)).where(Source.project_id.in_(owned_ids))) or 0)
    storage_bytes += (db.scalar(select(func.sum(Clip.size)).where(Clip.project_id.in_(owned_ids))) or 0)
    storage_bytes += totals.get("stored_bytes", 0)
    pricing = (("transcription_minutes", "STT_COST_PER_MINUTE", 1),
               ("prompt_tokens", "LLM_COST_PER_MILLION_PROMPT_TOKENS", 1_000_000),
               ("completion_tokens", "LLM_COST_PER_MILLION_COMPLETION_TOKENS", 1_000_000))
    costs = [(totals.get(metric, 0), os.getenv(variable), unit) for metric, variable, unit in pricing]
    estimated = (sum(amount * float(price or 0) / unit for amount, price, unit in costs)
                 if any(price is not None for _, price, _ in costs)
                 and all(not amount or price is not None for amount, price, _ in costs) else None)
    return {"processed": len({job.project_id for job in completed if job.kind == "analyze"}), "completed": len(completed),
            "estimated_cost_usd": estimated,
            "failed": sum(job.status == "failed" for job in jobs), "active": sum(job.status in ("running", "queued") for job in jobs),
            "clips": db.scalar(select(func.count()).select_from(Clip).where(Clip.project_id.in_(owned_ids), Clip.key.is_not(None))),
            "storage_bytes": storage_bytes, "processing_minutes": sum(j.seconds or 0 for j in jobs)/60,
            "average_seconds": sum(j.seconds or 0 for j in completed)/max(1, len(completed)),
            "usage": totals, "models": models}


@app.get("/publishing")
def publishing(user=Depends(current)):
    return {"mode": "manual", "platforms": ["YouTube Shorts", "Instagram Reels", "TikTok"], "connections": [], "jobs": []}


@app.get("/settings")
def settings(user=Depends(current)):
    return {"stt": os.getenv("STT_PROVIDER", "remote"), "transcription_ready": bool(os.getenv("STT_API_KEY")) or os.getenv("STT_PROVIDER") == "local",
            "discovery_ready": bool(os.getenv("LLM_API_KEY")) or local_discovery(), "url_ready": url_mode() != "disabled", "url_mode": url_mode(),
            "max_upload_mb": config.MAX_BYTES // 1024**2, "max_clips": config.MAX_CLIPS,
            "max_duration": config.MAX_DURATION, "max_daily_jobs": config.MAX_DAILY, "max_active_jobs": config.MAX_ACTIVE}
