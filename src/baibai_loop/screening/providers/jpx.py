from __future__ import annotations

import csv
from io import BytesIO
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse
from pathlib import Path
from typing import Mapping, Sequence

import requests

from ..schema import normalize_ticker


class JPXProviderError(RuntimeError):
    """Raised when required JPX public CSV/Excel sources are unavailable."""


@dataclass(frozen=True)
class JPXRegulationSnapshot:
    flags_by_ticker: Mapping[str, tuple[str, ...]]
    source_names: Sequence[str]


class JPXProvider:
    """Public CSV/Excel-only provider. HTML scraping is intentionally unsupported."""

    def __init__(
        self,
        cache_dir: Path,
        regulation_urls: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) / "jpx"
        self._session = session or requests.Session()
        self._regulation_urls = dict(regulation_urls or {})

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        cache_path = self._cache_dir / "regulations" / f"{asof_date.isoformat()}.json"
        if cache_path.exists():
            import json

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return JPXRegulationSnapshot(
                flags_by_ticker={ticker: tuple(flags) for ticker, flags in payload.get("flags_by_ticker", {}).items()},
                source_names=tuple(payload.get("source_names", ())),
            )

        if not self._regulation_urls:
            raise JPXProviderError("JPX regulation data is required but no public CSV/Excel URL is configured")

        flags: dict[str, set[str]] = {}
        for source_name, url in self._regulation_urls.items():
            rows = self._download_rows(source_name, url)
            for row in rows:
                ticker_raw = row.get("ticker") or row.get("code") or row.get("銘柄コード") or row.get("コード")
                flag = row.get("flag") or row.get("規制区分") or row.get("status") or source_name
                if not ticker_raw:
                    continue
                ticker = parse_jpx_code(ticker_raw)
                flags.setdefault(ticker, set()).add(str(flag))

        snapshot = JPXRegulationSnapshot(
            flags_by_ticker={ticker: tuple(sorted(values)) for ticker, values in sorted(flags.items())},
            source_names=tuple(sorted(self._regulation_urls.keys())),
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        cache_path.write_text(
            json.dumps(
                {
                    "flags_by_ticker": snapshot.flags_by_ticker,
                    "source_names": list(snapshot.source_names),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return snapshot

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        snapshot = self.get_regulation_snapshot(asof_date)
        return {"regulated_tickers": len(snapshot.flags_by_ticker)}

    def _download_rows(self, source_name: str, url: str) -> list[dict[str, str]]:
        response = self._session.get(url, timeout=30)
        if response.status_code >= 400:
            raise JPXProviderError(f"failed to download JPX regulation source: {url}")
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix == ".csv":
            return self._parse_csv_rows(response.content, url)
        if suffix in {".xls", ".xlsx"}:
            return self._parse_excel_rows(source_name, response.content, url)
        raise JPXProviderError(f"unsupported JPX regulation source format: {url}")

    def _parse_csv_rows(self, content: bytes, url: str) -> list[dict[str, str]]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("cp932")
            except UnicodeDecodeError as exc:
                raise JPXProviderError(f"failed to decode JPX regulation source: {url}") from exc
        reader = csv.DictReader(text.splitlines())
        return [dict(row) for row in reader]

    def _parse_excel_rows(self, source_name: str, content: bytes, url: str) -> list[dict[str, str]]:
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise JPXProviderError("pandas is required to read JPX Excel sources") from exc

        try:
            if source_name == "特別注意銘柄":
                return self._parse_special_alert_margin_rows(pd, content, source_name, url)
            frame = pd.read_excel(BytesIO(content), dtype=str)
        except ImportError as exc:
            raise JPXProviderError(
                f"missing Excel reader dependency for JPX source: {url}"
            ) from exc
        except Exception as exc:
            raise JPXProviderError(f"failed to parse JPX Excel source: {url}") from exc

        normalized = frame.fillna("")
        return [
            {str(column): str(value).strip() for column, value in row.items()}
            for row in normalized.to_dict(orient="records")
        ]

    def _parse_special_alert_margin_rows(self, pd: object, content: bytes, source_name: str, url: str) -> list[dict[str, str]]:
        # Official JPX margin xls marks current "特別注意銘柄" rows with "○" in the
        # second column and stores the 5-char security code in the seventh column.
        # Keep this logic source-specific so the file layout remains understandable
        # from code and tests without relying on ad-hoc HTML scraping.
        frame = pd.read_excel(BytesIO(content), header=None, dtype=str).fillna("")
        rows: list[dict[str, str]] = []
        for _, raw_row in frame.iloc[7:].iterrows():
            values = [str(value).strip() for value in raw_row.tolist()]
            if len(values) <= 6 or values[1] != "○":
                continue
            rows.append({"code": values[6], "flag": source_name})
        return rows


def parse_jpx_code(code: object) -> str:
    raw = str(code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw)
    if len(raw) == 5 and raw[:4].isalnum() and raw.endswith("0"):
        return normalize_ticker(raw[:4])
    raise JPXProviderError(f"invalid JPX code: {code!r}")
