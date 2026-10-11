"""Reading client exports out of Supabase Storage.

The worker holds the service key, so it bypasses the storage policies the way
it bypasses row-level security: it acts for the system rather than for a
person. That makes this the most sensitive credential in the project.

Files are streamed to a temporary path rather than held in memory. A CRM
export of four hundred thousand rows is normal and the worker has to survive
several of them.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import requests

BUCKET = "client-exports"
CHUNK = 1 << 20  # 1 MiB


class StorageError(RuntimeError):
    """Reaching the file failed. The run fails and says so."""


class NotConfigured(StorageError):
    """A credential is missing. Not transient: retrying cannot help."""


def _config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        missing = ", ".join(n for n, v in (("SUPABASE_URL", url),
                                           ("SUPABASE_SERVICE_KEY", key)) if not v)
        raise NotConfigured(
            f"{missing} is not set, so client exports cannot be read. See the "
            f"comment at the top of worker.py.")
    return url.rstrip("/"), key


def check_configured() -> None:
    """Called at startup. Raises NotConfigured if a credential is missing."""
    _config()


def download(storage_path: str, on_progress=None) -> Path:
    """Fetch an object to a temporary file and return its path.

    The caller deletes it. Nothing is cached: a client export sitting on disk
    after the job finishes is a copy of their CRM nobody is tracking.
    """
    url, key = _config()
    endpoint = f"{url}/storage/v1/object/{BUCKET}/{storage_path}"

    try:
        response = requests.get(
            endpoint, headers={"apikey": key, "Authorization": f"Bearer {key}"},
            stream=True, timeout=120)
    except requests.RequestException as exc:
        raise StorageError(f"could not reach storage: {exc}") from exc

    if response.status_code == 404:
        raise StorageError(
            f"the file is recorded against the engagement but is not in "
            f"storage at {storage_path}")
    if response.status_code >= 400:
        raise StorageError(
            f"storage refused the request: {response.status_code} "
            f"{response.text[:200]}")

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".download")
    done = 0
    try:
        with handle:
            for chunk in response.iter_content(CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise

    return Path(handle.name)
