"""Keep local analysis artifacts private, atomic, bounded, and redacted."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

_AUTHORIZATION = re.compile(r"(?im)(authorization\s*:\s*)(?:bearer\s+)?[^\r\n]+")
_SENSITIVE_KEY_PATTERN = (
    r"(?:aws_(?:secret_access_key|access_key_id)|r2_(?:secret_access_key|access_key_id)|"
    r"edinet_api_key|jquants_api_key|cloudflare_api_token|client_secret|"
    r"api[-_]?key|access[-_]?key(?:[-_]?id)?|secret|token|password|cookie|credential)"
)
_QUOTED_SECRET = re.compile(rf"""(?i)(["']{_SENSITIVE_KEY_PATTERN}["']\s*:\s*["'])[^"']+(["'])""")
_SECRET = re.compile(rf"(?i)\b({_SENSITIVE_KEY_PATTERN})(\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)")
_MAX_LOG_BYTES = 2_000_000


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise ValueError(f"required executable is unavailable: {name}")
    return str(Path(resolved).resolve(strict=True))


def redact(text: str) -> str:
    text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
    text = _QUOTED_SECRET.sub(r"\1[REDACTED]\2", text)
    return _SECRET.sub(r"\1\2[REDACTED]", text)


def redact_argv(argv: Sequence[object]) -> list[object]:
    rendered: list[object] = []
    redact_next = False
    for value in argv:
        if redact_next:
            rendered.append("[REDACTED]")
            redact_next = False
            continue
        if not isinstance(value, str):
            rendered.append(value)
            continue
        rendered.append(redact(value))
        flag = value.lstrip("-").lower().replace("_", "-")
        redact_next = "=" not in value and any(
            marker in flag
            for marker in (
                "authorization",
                "api-key",
                "access-key",
                "client-secret",
                "secret",
                "token",
                "password",
                "cookie",
                "credential",
            )
        )
    return rendered


def ensure_private_dir(path: Path, *, root: Path | None = None) -> Path:
    lexical_root = (root or path).absolute()
    lexical = path.absolute()
    if root is not None and not lexical.is_relative_to(lexical_root):
        raise ValueError(f"path escapes state root: {path}")
    if lexical_root.exists() and lexical_root.is_symlink():
        raise ValueError(f"state root must not be a symlink: {lexical_root}")
    cursor = lexical_root
    relative = lexical.relative_to(lexical_root)
    for part in relative.parts:
        cursor /= part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(f"workspace path must not traverse a symlink: {cursor}")
    resolved_root = lexical_root.resolve(strict=False)
    resolved = lexical.resolve(strict=False)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"path escapes state root: {path}")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.chmod(0o700)
    return resolved


def write_bytes_atomic(path: Path, payload: bytes, *, root: Path) -> None:
    parent = ensure_private_dir(path.parent, root=root)
    destination = path.absolute()
    if destination.is_symlink():
        raise ValueError(f"refusing to replace symlink: {destination}")
    with tempfile.NamedTemporaryFile(dir=parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        temporary.chmod(0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(destination)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object, *, root: Path) -> None:
    write_bytes_atomic(path, canonical_json(value) + b"\n", root=root)


def read_json(path: Path, *, root: Path) -> object:
    lexical_root = root.absolute()
    lexical = path.absolute()
    if not lexical.is_relative_to(lexical_root):
        raise ValueError(f"path escapes state root: {path}")
    cursor = lexical_root
    for part in lexical.relative_to(lexical_root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"refusing to read symlink path: {cursor}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"path escapes state root: {path}")
    return json.loads(resolved.read_text(encoding="utf-8"))


def write_log(path: Path, text: str, *, root: Path) -> bool:
    payload = redact(text).encode()
    truncated = len(payload) > _MAX_LOG_BYTES
    if truncated:
        payload = payload[-_MAX_LOG_BYTES:]
    write_bytes_atomic(path, payload, root=root)
    return truncated


__all__ = [
    "canonical_json",
    "ensure_private_dir",
    "read_json",
    "redact",
    "redact_argv",
    "resolve_executable",
    "write_bytes_atomic",
    "write_json_atomic",
    "write_log",
]
