import os
from pathlib import Path


DATABASE = os.getenv("DATABASE_URL", "sqlite:///./clipforge.sqlite")
REDIS = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STORAGE = Path(os.getenv("MEDIA_ROOT", "storage")).resolve()
SECRET = os.getenv("APP_SECRET", "")
SECURE = os.getenv("COOKIE_SECURE") == "1"
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "1024")) * 1024**2
MAX_DURATION = int(os.getenv("MAX_VIDEO_SECONDS", "7200"))
MAX_ACTIVE = int(os.getenv("MAX_ACTIVE_JOBS", "2"))
MAX_DAILY = int(os.getenv("MAX_DAILY_JOBS", "25"))
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "10"))
SESSION_SECONDS = 7 * 86400
ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:3000")
