"""Where uploaded files and spreadsheet table files live.

Two backends behind one small interface:

* **local** (default): files sit under the data directory, exactly as before.
* **s3**: any S3-compatible object store (AWS S3, Cloudflare R2, Backblaze B2, a self-hosted server). Several hosts can
  then share the same uploads and spreadsheet tables.

Everything the application stores is addressed by a *reference* string kept in the database
(``documents.path``, ``data_tables.file_key``):

* ``s3:<key>``              an object in the configured bucket (``S3_PREFIX`` is added in front by the backend);
* anything else             a path on this host's disk (absolute, or relative to the data directory). Documents uploaded
                            before object storage existed keep their absolute path, so nothing has to be migrated.

A reference decides which backend serves it, so switching ``STORAGE_BACKEND`` only changes where *new* files go; older
documents keep working (``python -m app.admin migrate-storage`` moves them across).

Extraction needs a real file: ``local_copy(ref)`` hands out a path, downloading to a temporary directory for an object and
removing it afterwards. Spreadsheet table files are immutable objects (each write gets a new key), so a copy cached on a
host's disk can never be stale; ``ensure_local`` downloads it on first use.
"""
import contextlib
import logging
import shutil
import tempfile
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from .config import settings

logger = logging.getLogger("mragrag")

S3_SCHEME = "s3:"


class StorageError(RuntimeError):
    """The storage backend could not do what was asked (misconfigured, unreachable, refused)."""


# ---- backends ---------------------------------------------------------------------------------

class LocalStorage:
    """Files under a root directory. Keys are relative paths."""
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / _safe_key(key)

    def put_bytes(self, key: str, data: bytes) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def put_file(self, key: str, source: Path) -> None:
        target = self.path(key)
        if Path(source).resolve() == target.resolve():
            return   # the file was written where it belongs
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def download(self, key: str, dest: Path) -> None:
        source = self.path(key)
        if not source.is_file():
            raise FileNotFoundError(key)
        if source.resolve() != Path(dest).resolve():
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> int:
        target = self.path(prefix)
        if target.is_dir():
            count = sum(1 for p in target.rglob("*") if p.is_file())
            shutil.rmtree(target, ignore_errors=True)
            return count
        return 0

    def keys(self, prefix: str = "") -> list[str]:
        base = self.root / _safe_key(prefix) if prefix else self.root
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())


class S3Storage:
    """An S3-compatible bucket. ``boto3`` is imported here only, so a local install never needs it."""
    name = "s3"

    def __init__(self, *, bucket: str, endpoint_url: str = "", region: str = "", access_key_id: str = "",
                 secret_access_key: str = "", prefix: str = "", path_style: bool = False, create_bucket: bool = False,
                 client=None):
        if not bucket:
            raise StorageError("S3_BUCKET is not set.")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.client = client or self._make_client(endpoint_url, region, access_key_id, secret_access_key, path_style)
        if create_bucket:
            self.ensure_bucket()

    @staticmethod
    def _make_client(endpoint_url, region, access_key_id, secret_access_key, path_style):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:   # pragma: no cover - boto3 is in requirements.txt
            raise StorageError("The S3 backend needs boto3 (pip install boto3).") from exc
        return boto3.client(
            "s3", endpoint_url=endpoint_url or None, region_name=region or "us-east-1",
            aws_access_key_id=access_key_id or None, aws_secret_access_key=secret_access_key or None,
            config=Config(s3={"addressing_style": "path" if path_style else "auto"}, signature_version="s3v4",
                          retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=60),
        )

    def _key(self, key: str) -> str:
        return self.prefix + _safe_key(key)

    @staticmethod
    def _code(exc) -> str:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if self._code(exc) not in ("404", "NoSuchBucket", "NotFound"):
                raise StorageError(f"Cannot use bucket {self.bucket}: {self._code(exc) or exc}") from exc
            try:
                self.client.create_bucket(Bucket=self.bucket)
                logger.info("Created the object-storage bucket %s", self.bucket)
            except ClientError as created:   # another process starting at the same moment created it first
                if self._code(created) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise StorageError(f"Cannot create bucket {self.bucket}: {self._code(created) or created}") from created

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def put_file(self, key: str, source: Path) -> None:
        self.client.upload_file(str(source), self.bucket, self._key(key))

    def download(self, key: str, dest: Path) -> None:
        from botocore.exceptions import ClientError
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(f".{dest.name}.{uuid4().hex[:8]}.part")   # never leave a half-written file under the real name
        try:
            self.client.download_file(self.bucket, self._key(key), str(partial))
            partial.replace(dest)
        except ClientError as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise
        finally:
            partial.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))   # deleting a missing key is not an error in S3

    def keys(self, prefix: str = "") -> list[str]:
        found = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            found.extend(item["Key"][len(self.prefix):] for item in page.get("Contents", []))
        return sorted(found)

    def delete_prefix(self, prefix: str) -> int:
        keys = self.keys(prefix)
        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": self.prefix + k} for k in batch], "Quiet": True})
        return len(keys)


def _safe_key(key: str) -> str:
    parts = [p for p in str(key).replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise StorageError(f"Invalid storage key: {key!r}")
    return "/".join(parts)


# ---- selecting a backend ------------------------------------------------------------------------

def _local() -> LocalStorage:
    return LocalStorage(settings.data_dir)


@lru_cache(maxsize=1)
def _s3() -> S3Storage:
    if not settings.s3_bucket:
        raise StorageError("This document is stored in object storage, but S3_BUCKET is not configured.")
    return S3Storage(
        bucket=settings.s3_bucket, endpoint_url=settings.s3_endpoint_url, region=settings.s3_region,
        access_key_id=settings.s3_access_key_id, secret_access_key=settings.s3_secret_access_key,
        prefix=settings.s3_prefix, path_style=settings.s3_path_style, create_bucket=settings.s3_create_bucket)


def reset() -> None:
    """Forget the cached S3 client (after settings changed; used by tests)."""
    _s3.cache_clear()


def backend_name() -> str:
    return "s3" if settings.storage_backend.strip().lower() == "s3" else "local"


def active():
    """The backend new files are written to."""
    return _s3() if backend_name() == "s3" else _local()


def check() -> None:
    """Fail early (at start-up) when the chosen backend cannot be used."""
    if backend_name() == "s3":
        backend = _s3()
        backend.client.head_bucket(Bucket=backend.bucket)


def is_object(ref: str) -> bool:
    return str(ref).startswith(S3_SCHEME)


def _split(ref: str):
    """``(backend, key)`` for a reference. A local reference's key is its path."""
    ref = str(ref)
    if is_object(ref):
        return _s3(), ref[len(S3_SCHEME):]
    return _local(), ref


def make_ref(key: str) -> str:
    """The reference for ``key`` on the active backend. Local references are paths relative to the data directory."""
    return S3_SCHEME + key if backend_name() == "s3" else key


def local_path(ref: str) -> Path:
    """Where a local reference lives on this host (absolute paths as stored by older versions are honoured)."""
    path = Path(str(ref))
    return path if path.is_absolute() else settings.data_dir / path


# ---- uploads ------------------------------------------------------------------------------------

def save_upload(name: str, raw: bytes) -> str:
    """Store an uploaded file and return its reference."""
    key = f"uploads/{uuid4()}_{name}"
    backend = active()
    backend.put_bytes(key, raw)
    return str(backend.path(key)) if backend.name == "local" else make_ref(key)


@contextlib.contextmanager
def local_copy(ref: str) -> Iterator[Path]:
    """A readable path to the stored file; an object is downloaded to a temporary directory that is removed afterwards."""
    if not is_object(ref):
        path = local_path(ref)
        if not path.is_file():
            raise FileNotFoundError(f"The stored file is missing: {path.name}")
        yield path
        return
    backend, key = _split(ref)
    scratch = settings.data_dir / "tmp" / uuid4().hex
    path = scratch / key.rsplit("/", 1)[-1]
    try:
        try:
            backend.download(key, path)
        except FileNotFoundError:
            raise FileNotFoundError(f"The stored file is missing: {key.rsplit('/', 1)[-1]}") from None
        yield path
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def delete(ref: str) -> None:
    """Remove a stored file. A failure is logged, never raised: once the rows are gone the file is unreachable."""
    if not ref:
        return
    try:
        backend, key = _split(ref)
        if backend.name == "local":
            local_path(ref).unlink(missing_ok=True)
        else:
            backend.delete(key)
    except Exception as exc:
        logger.warning("Could not delete stored file %s: %s: %s", str(ref)[-60:], type(exc).__name__, str(exc)[:120])


def delete_prefix(prefix: str) -> None:
    """Remove every object under ``prefix`` from the active backend (best effort)."""
    try:
        active().delete_prefix(prefix)
    except Exception as exc:
        logger.warning("Could not delete stored files under %s: %s: %s", prefix, type(exc).__name__, str(exc)[:120])


# ---- derived files (spreadsheet tables) ---------------------------------------------------------

def publish(key: str, source: Path) -> str:
    """Store a file that was just built on this host and return its reference. The local copy stays as the cache."""
    active().put_file(key, source)
    return make_ref(key)


def ensure_local(ref: str) -> Path:
    """The file of a reference on this host's disk, downloading an object into the cache the first time."""
    if not is_object(ref):
        path = local_path(ref)
        if not path.is_file():
            raise FileNotFoundError(path.name)
        return path
    backend, key = _split(ref)
    path = settings.data_dir / _safe_key(key)
    if not path.is_file():
        backend.download(key, path)
    return path
