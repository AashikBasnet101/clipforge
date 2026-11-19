# Deployment notes

## Service layout

Serve the frontend and API behind TLS on the same site, with a fixed frontend origin. Set `APP_ORIGIN` to the frontend URL, `NEXT_PUBLIC_API_URL` to the browser-facing API URL before building the frontend, and `COOKIE_SECURE=1`. Publish only the TLS gateway. The Compose ports bind to local loopback. Constrain proxy request bodies to the configured upload budget and use appropriate upload timeouts. Serve media requests with range support and avoid access logs containing signed URL query strings.

Use a dedicated PostgreSQL role, protected persistent Redis with authentication and private networking, and a durable media volume or private S3-compatible bucket. Supply S3 credentials through the standard SDK credential chain. Set `S3_BUCKET` and optionally `S3_ENDPOINT_URL`. The endpoint returned in presigned URLs must be reachable by the browser. Browser sessions are verified before signed links are issued or redeemed. S3 URLs are valid for three minutes after the authenticated redirect.

Set `MAX_UPLOAD_MB`, `MAX_VIDEO_SECONDS`, `MAX_DAILY_JOBS`, `MAX_ACTIVE_JOBS`, and `MAX_CLIPS` for your budget. Store PostgreSQL backups, test restores, and keep Redis persistence enabled. Operate a single beat scheduler. Run migrations once before scaling the API. Keep the worker isolated, unprivileged, and resource-limited. Pin and test container digests as part of your release process.

## Operations

- The API requires a strong `APP_SECRET`. Changing it invalidates existing media signatures. Application sessions expire after seven days and can be revoked through logout.
- Redis enforces registration and login rate limits. Preserve a trusted proxy configuration if deriving client addresses at a proxy.
- Temporary render assets use process-scoped directories. Production workers should use an ephemeral temporary filesystem and a retention policy for orphaned objects after abnormal termination.
- A cancelled job stops at the next pipeline checkpoint. A native solo worker cannot interrupt transcription or encoding mid-call. The API keeps the previous completed export available after a failed rerender.
- FFmpeg uses explicit container demuxers and restricts input protocols. Keep FFmpeg and its codecs patched. For untrusted public uploads, add a dedicated sandbox or VM for the media subprocesses and network controls for worker egress.
- URL ingestion uses public address validation, a pinned TCP destination and TLS hostname verification. Keep an exact `MEDIA_URL_HOSTS` allowlist on shared deployments. `ALLOW_PUBLIC_MEDIA_URLS=1` is an opt-in for trusted local users. Redirects are disabled. Restricted platforms are supported through user-provided, authorized files.
- Text discovery uses transcript signals. OpenCV face-based crop is optional and reports when it fits the full source frame into the target aspect ratio. Media needs a video and an audio stream.
- Storage usage reports managed source, preview and current export sizes. Download counts are requests. Platform analytics and invoice-level cost reconciliation are unavailable.
