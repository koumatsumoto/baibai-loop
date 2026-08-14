"""Crash-consistent installation primitives for immutable lake files."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path


class ImmutableInstallError(RuntimeError):
    pass


def install_immutable_bytes(
    path: Path,
    payload: bytes,
    *,
    validate: Callable[[bytes], object] | None = None,
) -> bool:
    """Install complete bytes without replacing an existing immutable object."""
    if validate is not None:
        validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        if validate is not None:
            validate(temporary.read_bytes())
        try:
            os.link(temporary, path)
            _fsync_directory(path.parent)
            return True
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ImmutableInstallError(f"immutable file already differs: {path}") from None
            return False
    finally:
        temporary.unlink(missing_ok=True)


def install_immutable_file(path: Path, captured: Path, *, expected_sha256: str) -> bool:
    """Link a fully captured file into place without replacing another inode."""
    from .sources import sha256_file

    if sha256_file(captured) != expected_sha256:
        raise ImmutableInstallError(f"captured file digest differs before install: {captured}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(captured, path)
        _fsync_directory(path.parent)
        return True
    except FileExistsError:
        if sha256_file(path) != expected_sha256:
            raise ImmutableInstallError(f"immutable file already differs: {path}") from None
        return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
