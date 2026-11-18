"""Keeping files (bill PDFs now; catalogues and product photos later).

Files are stored in the database because the free host wipes its disk on every restart. Everything
goes through save() / load(), so moving to cloud storage later only changes this module.
"""

import hashlib

from sqlalchemy.orm import Session

from app.models import StoredFile

MAX_BYTES = 5 * 1024 * 1024  # 5 MB: plenty for a bill PDF, small enough for the free database


class StorageError(ValueError):
    """Shown to the owner as-is."""


def save(db: Session, kind: str, filename: str, content_type: str, data: bytes) -> StoredFile:
    if not data:
        raise StorageError(f"{filename} is empty")
    if len(data) > MAX_BYTES:
        raise StorageError(f"{filename} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    f = StoredFile(kind=kind, filename=filename[:255], content_type=content_type, size=len(data),
                   sha256=hashlib.sha256(data).hexdigest(), data=data)
    db.add(f)
    db.flush()
    return f


def load(db: Session, file_id: int) -> StoredFile | None:
    return db.get(StoredFile, file_id)
