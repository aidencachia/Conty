"""Stable, owner-only PDF export location for PowerPoint's sandbox folder grant.

Keep the directory and lock inode across exports/restarts, but remove the PDF.
No native APIs, permission prompts, or filesystem operations occur at import.
"""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from conty.adapters.base import AdapterError


def _failure():
    return AdapterError("export_failed", "PowerPoint preview workspace is unavailable or unsafe")


def default_export_directory():
    return Path.home() / "Library" / "Application Support" / "Conty" / "PowerPoint Previews"


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise _failure()


def _regular_owned(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1


def _remove_pdf(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    # Never follow symlinks or remove a directory, hard link, or foreign-owned file.
    if not _regular_owned(info):
        raise _failure()
    path.unlink()


def secure_pdf(path, maximum_size):
    """Check the exported artifact without following links, then restrict its mode."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not _regular_owned(info) or not 0 < info.st_size <= maximum_size:
                raise _failure()
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except OSError:
        raise _failure() from None


@contextmanager
def export_workspace(directory=None):
    """Serialize exports to one stable filename; never remove the permission-granted folder."""
    try:
        if directory is None:
            directory = default_export_directory()
            # Validate Conty's own root before making children, including on reuse.
            _private_directory(directory.parent)
        else:
            directory = Path(directory)
        if not directory.is_absolute():
            raise _failure()
        _private_directory(directory)
        descriptor = os.open(
            directory / ".export.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not _regular_owned(info) or info.st_mode & 0o077:
                raise _failure()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AdapterError(
                    "export_busy", "Another PowerPoint preview export is in progress"
                ) from None
            pdf = directory / "preview.pdf"
            _remove_pdf(pdf)  # Remove only our stale artifact left by an interrupted export.
            try:
                yield pdf
            except BaseException:
                # Cleanup must not hide an uncertain native effect or cancellation.
                try:
                    _remove_pdf(pdf)
                except (OSError, AdapterError):
                    pass
                raise
            else:
                _remove_pdf(pdf)
        finally:
            # Closing releases this export's lock, including after errors/cancellation.
            os.close(descriptor)
    except OSError:
        raise _failure() from None
