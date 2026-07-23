# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#   "playwright==1.61.0",
#   "pypdf==6.14.2",
#   "pyyaml==6.0.3",
#   "requests==2.34.2",
# ]
# ///

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from calendar import month_name
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml
from playwright.sync_api import (
    BrowserContext,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from pmi_text import extract_pmi_value, latest_seed_matches
from pypdf import PdfReader

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY = "https://web.archive.org/web/{timestamp}id_/{original}"
MAX_PMI_PDF_BYTES = 5 * 1024 * 1024
MAX_CDX_RESPONSE_BYTES = 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MONTHS = "|".join(month_name[1:])
PMI_OBSERVATION_RES = (
    re.compile(
        rf"\b(?P<month>{MONTHS})\s+(?P<year>20\d{{2}})\s+data were collected\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bdata were collected\s+[\d–—-]+\s+"
        rf"(?P<month>{MONTHS})\s+(?P<year>20\d{{2}})\b",
        re.IGNORECASE,
    ),
)
_CACHE_DIR: Path | None = None


def _is_pdf(content: bytes) -> bool:
    if len(content) > MAX_PMI_PDF_BYTES:
        raise RuntimeError(f"PMI PDF exceeds {MAX_PMI_PDF_BYTES} bytes")
    return content.startswith(b"%PDF")


class BrowserPdfFetcher:
    """Fetch official PDFs with a real browser when the source WAF requires JavaScript."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._profile: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> BrowserPdfFetcher:
        return self

    def _start(self) -> None:
        if self._context is not None or self._playwright is not None:
            return
        executable = shutil.which("google-chrome") or shutil.which("chromium")
        if executable is None:
            return
        self._profile = tempfile.TemporaryDirectory(prefix="baibai-pmi-browser-")
        preferences = Path(self._profile.name) / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True)
        preferences.write_text(
            json.dumps(
                {
                    "download": {"prompt_for_download": False},
                    "plugins": {"always_open_pdf_externally": True},
                }
            ),
            encoding="utf-8",
        )
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            self._profile.name,
            executable_path=executable,
            headless=True,
            accept_downloads=True,
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
        )

    def __exit__(self, *_args: object) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        if self._profile is not None:
            self._profile.cleanup()

    def fetch(self, url: str) -> bytes | None:
        self._start()
        if self._context is None:
            return None
        page = self._context.new_page()
        try:
            for _attempt in range(2):
                try:
                    with (
                        page.expect_download(timeout=60_000) as download_info,
                        suppress(PlaywrightError),
                    ):
                        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    download_path = download_info.value.path()
                except PlaywrightTimeoutError:
                    continue
                if download_path is not None:
                    path = Path(download_path)
                    if path.stat().st_size > MAX_PMI_PDF_BYTES:
                        raise RuntimeError(f"PMI PDF exceeds {MAX_PMI_PDF_BYTES} bytes")
                    return path.read_bytes()
        finally:
            page.close()
        return None


def _request(session: requests.Session, url: str, **kwargs: object) -> requests.Response:
    for attempt in range(2):
        try:
            response = session.get(url, timeout=30, **kwargs)
        except requests.RequestException:
            if attempt == 1:
                raise
            time.sleep(2**attempt)
            continue
        if response.status_code not in {429, 500, 502, 503, 504}:
            try:
                response.raise_for_status()
            except requests.RequestException:
                response.close()
                raise
            if url.startswith("https://web.archive.org/"):
                time.sleep(1)
            return response
        response.close()
        time.sleep(2**attempt)
    response.raise_for_status()
    raise AssertionError("unreachable")


def _cache_path(kind: str, key: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    target = _CACHE_DIR / kind
    target.mkdir(parents=True, exist_ok=True)
    return target / key


def _read_cached(path: Path, *, max_bytes: int) -> bytes:
    if path.stat().st_size > max_bytes:
        raise RuntimeError(f"cached response exceeds {max_bytes} bytes: {path}")
    return path.read_bytes()


def _response_bytes(response: requests.Response, *, max_bytes: int) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise RuntimeError(f"invalid response Content-Length: {raw_length!r}") from exc
        if content_length > max_bytes:
            raise RuntimeError(f"response exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_bytes(
    session: requests.Session,
    url: str,
    *,
    max_bytes: int,
    **kwargs: object,
) -> bytes:
    response = _request(session, url, stream=True, **kwargs)
    try:
        return _response_bytes(response, max_bytes=max_bytes)
    finally:
        response.close()


def _cdx(
    session: requests.Session,
    *,
    url: str,
    start: date,
    end: date,
) -> list[dict[str, str]]:
    params = [
        ("url", url),
        ("from", start.strftime("%Y%m%d")),
        ("to", end.strftime("%Y%m%d")),
        ("fl", "timestamp,original,mimetype"),
        ("output", "json"),
        ("limit", "5000"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:application/pdf"),
        ("collapse", "digest"),
    ]
    cache_key = hashlib.sha256(f"{url!r}:{params!r}".encode()).hexdigest()
    cache_path = _cache_path("cdx", cache_key)
    if cache_path is not None and cache_path.exists():
        payload = json.loads(_read_cached(cache_path, max_bytes=MAX_CDX_RESPONSE_BYTES).decode())
    else:
        content = _request_bytes(
            session,
            WAYBACK_CDX_URL,
            params=params,
            max_bytes=MAX_CDX_RESPONSE_BYTES,
        )
        payload = json.loads(content)
        if cache_path is not None:
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    if not isinstance(header, list):
        raise RuntimeError("CDX header is not a list")
    return [
        dict(zip((str(item) for item in header), (str(item) for item in row), strict=True))
        for row in payload[1:]
        if isinstance(row, list)
    ]


def _snapshot(session: requests.Session, *, timestamp: str, original: str) -> bytes:
    cache_key = hashlib.sha256(f"{timestamp}:{original}".encode()).hexdigest()
    cache_path = _cache_path("snapshot", cache_key)
    if cache_path is not None and cache_path.exists():
        return _read_cached(cache_path, max_bytes=MAX_PMI_PDF_BYTES)
    replay = WAYBACK_REPLAY.format(timestamp=timestamp, original=original)
    content = _request_bytes(
        session,
        replay,
        max_bytes=MAX_PMI_PDF_BYTES,
    )
    if cache_path is not None:
        cache_path.write_bytes(content)
    return content


def _month_floor(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def _months(start: date, end: date) -> list[date]:
    result: list[date] = []
    current = _month_floor(start)
    while current <= end:
        result.append(current)
        current = _next_month(current)
    return result


def _load_release_urls(path: Path) -> dict[date, tuple[str, date]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RuntimeError("PMI release URL manifest must use schema_version 1")
    releases = raw.get("releases")
    if not isinstance(releases, list):
        raise RuntimeError("PMI release URL manifest releases must be a list")
    result: dict[date, tuple[str, date]] = {}
    for index, item in enumerate(releases):
        if not isinstance(item, dict):
            raise RuntimeError(f"PMI release URL entry {index} must be a mapping")
        observed_at = date.fromisoformat(str(item.get("observed_at")))
        release_observed_at = date.fromisoformat(str(item.get("release_observed_at", observed_at)))
        if observed_at not in {
            release_observed_at,
            _previous_month(release_observed_at),
        }:
            raise RuntimeError(
                f"PMI release for {observed_at} must cover that month or the following month"
            )
        url = str(item.get("url", ""))
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "www.pmi.spglobal.com"
            or not re.fullmatch(r"/Public/Home/PressRelease/[0-9a-f]{32}", parts.path)
            or parts.query
            or parts.fragment
        ):
            raise RuntimeError(f"invalid official PMI release URL for {observed_at}: {url}")
        if observed_at in result:
            raise RuntimeError(f"duplicate PMI release URL entry: {observed_at}")
        result[observed_at] = (url, release_observed_at)
    return result


def _extract_pmi(
    pdf: bytes,
    *,
    expected_observed_at: date,
    expected_release_observed_at: date,
) -> float | None:
    if not _is_pdf(pdf):
        return None
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages[:2])
    normalized = " ".join(text.split())
    if "Japan Manufacturing PMI" not in normalized or "Flash Japan" in normalized:
        return None
    observation_match = next(
        (
            match
            for pattern in PMI_OBSERVATION_RES
            if (match := pattern.search(normalized)) is not None
        ),
        None,
    )
    if observation_match is None:
        return None
    release_observed_at = date(
        int(observation_match.group("year")),
        list(month_name).index(observation_match.group("month").title()),
        1,
    )
    if release_observed_at != expected_release_observed_at:
        return None
    if expected_observed_at not in {
        release_observed_at,
        _previous_month(release_observed_at),
    }:
        return None
    return extract_pmi_value(
        normalized,
        expected_observed_at=expected_observed_at,
        release_observed_at=release_observed_at,
    )


def _release_pdf(
    session: requests.Session,
    *,
    official_url: str,
    around: date,
    browser: BrowserPdfFetcher,
) -> bytes:
    direct_cache = _cache_path(
        "official",
        hashlib.sha256(official_url.encode()).hexdigest(),
    )
    if direct_cache is not None and direct_cache.exists():
        current = _read_cached(direct_cache, max_bytes=MAX_PMI_PDF_BYTES)
    else:
        try:
            current = _request_bytes(
                session,
                official_url,
                max_bytes=MAX_PMI_PDF_BYTES,
            )
        except requests.RequestException:
            current = b""
        if _is_pdf(current) and direct_cache is not None:
            direct_cache.write_bytes(current)
    if _is_pdf(current):
        return current
    try:
        captures = _cdx(
            session,
            url=official_url,
            start=around - timedelta(days=10),
            end=around + timedelta(days=45),
        )
    except requests.RequestException as exc:
        print(f"release archive lookup skipped: {exc}", file=sys.stderr, flush=True)
        captures = []
    for capture in captures:
        try:
            pdf = _snapshot(
                session,
                timestamp=capture["timestamp"],
                original=capture["original"],
            )
        except requests.RequestException as exc:
            print(
                f"release snapshot skipped {capture['timestamp']}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        if _is_pdf(pdf):
            return pdf
    pdf = browser.fetch(official_url)
    if pdf is not None and _is_pdf(pdf):
        if direct_cache is not None:
            direct_cache.write_bytes(pdf)
        return pdf
    raise RuntimeError(f"official PMI PDF unavailable: {official_url}")


def _backfill(
    session: requests.Session,
    *,
    expected: list[date],
    release_urls: dict[date, tuple[str, date]],
    browser: BrowserPdfFetcher,
) -> dict[date, tuple[float, str]]:
    missing_urls = [observed_at for observed_at in expected if observed_at not in release_urls]
    if missing_urls:
        raise RuntimeError(
            "PMI release URL manifest is missing months: "
            + ", ".join(str(item) for item in missing_urls)
        )
    values: dict[date, tuple[float, str]] = {}
    for observed_at in expected:
        official_url, release_observed_at = release_urls[observed_at]
        pdf = _release_pdf(
            session,
            official_url=official_url,
            around=_next_month(release_observed_at),
            browser=browser,
        )
        value = _extract_pmi(
            pdf,
            expected_observed_at=observed_at,
            expected_release_observed_at=release_observed_at,
        )
        if value is None:
            raise RuntimeError(f"failed to extract {observed_at} from {official_url}")
        values[observed_at] = (value, official_url)
        print(f"PMI {observed_at} {value}", file=sys.stderr, flush=True)
    return values


def _write_seed(
    output: Path,
    *,
    pmi: dict[date, tuple[float, str]],
) -> None:
    if not pmi:
        raise RuntimeError("refusing to write an empty PMI seed")
    observed_dates = sorted(pmi)
    if observed_dates != _months(observed_dates[0], observed_dates[-1]):
        raise RuntimeError("refusing to write a PMI seed with missing months")

    existing: list[dict[str, object]] = []
    if output.exists():
        raw = yaml.safe_load(output.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise RuntimeError("existing PMI seed must use schema_version 1")
        raw_observations = raw.get("observations")
        if not isinstance(raw_observations, list):
            raise RuntimeError("existing PMI seed observations must be a list")
        for item in raw_observations:
            if not isinstance(item, dict):
                raise RuntimeError("existing PMI seed entry must be a mapping")
            if item.get("series_id") != "jp.pmi_manufacturing":
                raise RuntimeError("existing seed contains a non-PMI series")
            existing.append(dict(item))

    now = datetime.now(UTC).isoformat()
    replaced_dates = {str(item) for item in pmi}
    observations = [item for item in existing if str(item.get("observed_at")) not in replaced_dates]
    for observed_at, (value, source_url) in sorted(pmi.items()):
        same_date = [item for item in existing if str(item.get("observed_at")) == str(observed_at)]
        observations.extend(same_date)
        if not latest_seed_matches(same_date, value=value, source_url=source_url):
            observations.append(
                {
                    "series_id": "jp.pmi_manufacturing",
                    "observed_at": observed_at,
                    "value": value,
                    "unit": "index",
                    "source_url": source_url,
                    "entered_at": now,
                }
            )
    observations.sort(key=lambda item: (str(item.get("observed_at")), str(item.get("entered_at"))))
    body = yaml.safe_dump(
        {"schema_version": 1, "observations": observations},
        allow_unicode=True,
        sort_keys=False,
    )
    rendered = (
        "# Canonical observations for manual series with no stable free API.\n"
        "# Values are mechanically extracted from the cited primary-source releases.\n"
        f"{body}"
    )
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict) or len(parsed.get("observations", [])) != len(observations):
        raise RuntimeError("generated PMI seed failed validation")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(output)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    global _CACHE_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--release-urls",
        type=Path,
        default=(
            Path(__file__).parents[2]
            / "src/baibai_engine/macro/indicators/providers/pmi_release_urls.yaml"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")
    expected = _months(args.start, args.end)
    if not expected:
        parser.error("requested PMI range must contain at least one month")
    release_urls = _load_release_urls(args.release_urls)
    canonical_output = (
        Path(__file__).parents[2] / "src/baibai_engine/macro/indicators/providers/manual_data.yaml"
    )
    if args.output.resolve() == canonical_output.resolve() and set(expected) != set(release_urls):
        parser.error("canonical PMI seed requires the complete release manifest range")
    temporary_cache: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.cache_dir is None:
            temporary_cache = tempfile.TemporaryDirectory(prefix="baibai-pmi-cache-")
            _CACHE_DIR = Path(temporary_cache.name)
        else:
            _CACHE_DIR = args.cache_dir
        with requests.Session() as session:
            session.headers["User-Agent"] = USER_AGENT
            session.headers["Accept"] = "application/pdf,*/*"
            with BrowserPdfFetcher() as browser:
                pmi = _backfill(
                    session,
                    expected=expected,
                    release_urls=release_urls,
                    browser=browser,
                )
    finally:
        if temporary_cache is not None:
            temporary_cache.cleanup()
    _write_seed(args.output, pmi=pmi)
    print(
        json.dumps(
            {
                "pmi": len(pmi),
                "start": str(args.start),
                "end": str(args.end),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
