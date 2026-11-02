"""One-off PR experiment. Remove this file and the CI step before merging."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import urllib3

from baibai_engine.data_collection.transport.identity import PUBLIC_RESEARCH_USER_AGENT

ARTICLE_ACCEPT = "application/json, application/xml, text/html, application/pdf;q=0.9, */*;q=0.5"
LIMIT = 2 * 1024 * 1024


class ObservedSession(requests.Session):
    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        # No credentials are needed for this public experiment. Never log their values.
        if any(
            name.lower() in {"authorization", "proxy-authorization"} for name in request.headers
        ):
            raise ValueError("authenticated request is outside the experiment")
        return super().send(request, **kwargs)


def main() -> None:
    print(
        json.dumps(
            {
                "runtime": {
                    "python": platform.python_version(),
                    "requests": requests.__version__,
                    "urllib3": urllib3.__version__,
                    "openssl": ssl.OPENSSL_VERSION,
                },
                "proxy_environment_present": {
                    name: bool(os.environ.get(name))
                    for name in (
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "http_proxy",
                        "https_proxy",
                    )
                },
                "source_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "response_limit_bytes": LIMIT,
            },
            sort_keys=True,
        )
    )
    with ObservedSession() as warm:
        cases = [
            ("news_index", "https://minkabu.jp/news", ARTICLE_ACCEPT, warm),
            ("news_after_index", "https://minkabu.jp/news/4645703", ARTICLE_ACCEPT, warm),
            ("news_fresh", "https://minkabu.jp/news/4645703", ARTICLE_ACCEPT, None),
            ("news_html_accept", "https://minkabu.jp/news/4645703", "text/html", None),
            ("stock_html_control", "https://minkabu.jp/stock/7203", "text/html", None),
        ]
        for index, (name, url, accept, reused) in enumerate(cases):
            if index:
                time.sleep(2)
            session = reused or ObservedSession()
            row = {
                "case": name,
                "url": url,
                "session_reused": reused is not None,
                "cookies_before": len(session.cookies),
                "trust_env": session.trust_env,
                "tls_verification": session.verify is True,
                "requested_at": datetime.now(UTC).isoformat(),
            }
            try:
                with session.get(
                    url,
                    headers={"User-Agent": PUBLIC_RESEARCH_USER_AGENT, "Accept": accept},
                    timeout=(10, 20),
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    row.update(
                        status=response.status_code,
                        received_at=datetime.now(UTC).isoformat(),
                        http_version=response.raw.version,
                    )
                    row["prepared_headers"] = {
                        key: response.request.headers.get(key)
                        for key in ("User-Agent", "Accept", "Accept-Encoding", "Connection")
                    }
                    row["prepared_header_names"] = sorted(response.request.headers)
                    row["prepared_cookie_present"] = "Cookie" in response.request.headers
                    connection = getattr(response.raw, "connection", None)
                    sock = getattr(connection, "sock", None)
                    if sock is not None:
                        row["tls_version"] = sock.version()
                        cipher = sock.cipher()
                        row["tls_cipher"] = cipher[0] if cipher else None
                        row["tls_peer_sha256"] = hashlib.sha256(
                            sock.getpeercert(binary_form=True) or b""
                        ).hexdigest()
                    row["response_headers"] = {
                        key: response.headers.get(key)
                        for key in (
                            "Server",
                            "Via",
                            "Content-Type",
                            "Content-Encoding",
                            "X-Cache",
                            "X-Amz-Cf-Pop",
                        )
                    }
                    row["redirect_present"] = bool(response.headers.get("Location"))
                    content = bytearray()
                    complete = True
                    for chunk in response.iter_content(65536):
                        if len(content) + len(chunk) > LIMIT:
                            complete = False
                            break
                        content.extend(chunk)
                    row.update(
                        body_bytes=len(content),
                        complete_body=complete,
                        body_sha256=hashlib.sha256(content).hexdigest(),
                        cloudfront_marker=b"cloudfront" in content.lower(),
                        cookies_after=len(session.cookies),
                    )
            except Exception as error:
                row["error_type"] = type(error).__name__
            finally:
                if reused is None:
                    session.close()
            print(json.dumps(row, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
