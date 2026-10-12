"""Explicit DuckDB sessions for reading immutable lake objects.

A session owns one connection and closes it. There is no module-level connection,
so a caller cannot inherit another caller's extensions, settings, or secrets.

The R2 secret is created without ``PERSISTENT``, which keeps it in the instance's
memory for the life of the connection only. Credentials are bound as query
parameters instead of being formatted into SQL, so neither the statement text nor
a parse error can carry them. ``redact`` covers the remaining path: DuckDB's own
httpfs errors quote the request URL, and the endpoint host contains the account id.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import duckdb

_ACCOUNT_ID: Final = re.compile(r"^[0-9a-f]{32}$")
_ACCESS_KEY_ID: Final = re.compile(r"^[A-Za-z0-9]{16,128}$")
_BUCKET: Final = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SECRET_NAME: Final = "baibai_lake_r2"
_REDACTED: Final = "<redacted>"


class LakeCredentialError(RuntimeError):
    """R2 read credentials are absent or do not have the expected shape."""


@dataclass(frozen=True, slots=True)
class R2ReadCredentials:
    """Read-side R2 credentials. Never rendered, logged, or compared by value."""

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    def __post_init__(self) -> None:
        if _ACCOUNT_ID.fullmatch(self.account_id) is None:
            raise LakeCredentialError("R2_ACCOUNT_ID must be 32 lowercase hexadecimal characters")
        if _ACCESS_KEY_ID.fullmatch(self.access_key_id) is None:
            raise LakeCredentialError("R2_ACCESS_KEY_ID must be an alphanumeric access key id")
        if not self.secret_access_key:
            raise LakeCredentialError("R2_SECRET_ACCESS_KEY must not be empty")
        if _BUCKET.fullmatch(self.bucket) is None:
            raise LakeCredentialError("R2 bucket must be a lowercase DNS-safe name")

    def __repr__(self) -> str:
        return f"R2ReadCredentials(bucket={self.bucket!r})"

    __str__ = __repr__

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, bucket: str = "baibai-stores"
    ) -> R2ReadCredentials:
        source = os.environ if env is None else env
        return cls(
            account_id=_required(source, "R2_ACCOUNT_ID"),
            access_key_id=_required(source, "R2_ACCESS_KEY_ID"),
            secret_access_key=_required(source, "R2_SECRET_ACCESS_KEY"),
            bucket=bucket,
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise LakeCredentialError(f"required environment variable is missing: {name}")
    return value


@dataclass(frozen=True, slots=True)
class LakeSession:
    """One DuckDB connection plus the redaction its error paths need."""

    connection: duckdb.DuckDBPyConnection
    credentials: R2ReadCredentials | None

    def redact(self, message: str) -> str:
        """Replace credential-derived substrings, longest first.

        The endpoint host embeds the account id, so a message that never mentions a
        key can still identify the bucket owner.
        """

        if self.credentials is None:
            return message
        secrets = sorted(
            (
                self.credentials.secret_access_key,
                self.credentials.access_key_id,
                self.credentials.account_id,
            ),
            key=len,
            reverse=True,
        )
        for secret in secrets:
            message = message.replace(secret, _REDACTED)
        return message


@contextmanager
def lake_session(*, credentials: R2ReadCredentials | None = None) -> Iterator[LakeSession]:
    """Open a connection, optionally authorised for one R2 bucket, and close it.

    Extension autoload and autoinstall are switched off, so the only way this
    connection can speak HTTP is the explicit ``LOAD`` in ``_authorize_r2``.
    Deployment installs the pinned extension ahead of time; runtime never downloads
    executable code. Without credentials ``LOAD`` never runs, and a remote path
    handed to the connection by mistake fails instead of silently pulling in
    ``httpfs``.
    """

    connection = duckdb.connect(":memory:")
    connection.execute("SET autoinstall_known_extensions = false")
    connection.execute("SET autoload_known_extensions = false")
    session = LakeSession(connection=connection, credentials=credentials)
    try:
        if credentials is not None:
            _authorize_r2(session, credentials)
        yield session
    finally:
        connection.close()


def _authorize_r2(session: LakeSession, credentials: R2ReadCredentials) -> None:
    connection = session.connection
    try:
        connection.execute("LOAD httpfs")
        # Not PERSISTENT: the secret lives in this connection's memory only.
        connection.execute(
            f"CREATE OR REPLACE SECRET {_SECRET_NAME} "  # nosec B608 - fixed identifier
            "(TYPE r2, KEY_ID $key_id, SECRET $secret, ACCOUNT_ID $account_id)",
            {
                "key_id": credentials.access_key_id,
                "secret": credentials.secret_access_key,
                "account_id": credentials.account_id,
            },
        )
    except duckdb.Error as exc:
        raise LakeCredentialError(
            "R2 read session could not be authorised; deployment must preinstall "
            f"the DuckDB httpfs extension: {session.redact(str(exc))}"
        ) from None


def r2_uri(credentials: R2ReadCredentials, key: str) -> str:
    return f"r2://{credentials.bucket}/{key}"
