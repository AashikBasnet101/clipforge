import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import STORAGE


def local_path(key):
    if not re.fullmatch(r"[a-f0-9]{32}/[a-zA-Z0-9_.-]+", key) or key.endswith(("/.", "/..")):
        raise ValueError("Invalid asset key")
    path = STORAGE / key
    if not path.resolve().is_relative_to(STORAGE):
        raise ValueError("Invalid asset path")
    return path


def bucket():
    return os.getenv("S3_BUCKET", "")


def client():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=60))


def store(path, key, mime):
    dest = local_path(key)
    if bucket():
        client().upload_file(str(path), bucket(), key, ExtraArgs={"ContentType": mime})
    else:
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = dest.with_suffix(dest.suffix + ".part")
        try:
            shutil.copyfile(path, staging)
            staging.replace(dest)
        finally:
            staging.unlink(missing_ok=True)


@contextmanager
def materialize(key):
    path = local_path(key)
    if not bucket():
        yield path
        return
    with TemporaryDirectory() as temp:
        path = Path(temp) / "input.media"
        client().download_file(bucket(), key, str(path))
        yield path


def remove(key):
    path = local_path(key)
    if bucket():
        client().delete_object(Bucket=bucket(), Key=key)
    else:
        path.unlink(missing_ok=True)
