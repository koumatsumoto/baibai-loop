"""One finite comparison of ordinary public HTTP requests; never a collector fallback."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
import time
from datetime import UTC, datetime

import requests

from baibai_engine.data_collection.transport.identity import PUBLIC_RESEARCH_USER_AGENT

URL = "https://minkabu.jp/news"
ACCEPT = "application/json, application/xml, text/html, application/pdf;q=0.9, */*;q=0.5"


def main():
    variants = [
        ("identity-encoding", {"Accept-Encoding": "identity"}, True),
        ("connection-close", {"Connection": "close"}, True),
    ]
    for index, (name, extra, stream) in enumerate(variants):
        if index:
            time.sleep(6)
        headers = {"User-Agent": PUBLIC_RESEARCH_USER_AGENT, "Accept": ACCEPT, **extra}
        result = {"variant": name, "started_at": datetime.now(UTC).isoformat()}
        with requests.Session() as session:
            try:
                with session.get(
                    URL, headers=headers, stream=stream, timeout=20, allow_redirects=False
                ) as response:
                    body = bytearray()
                    for chunk in response.iter_content(16384):
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise ValueError("response exceeds diagnostic limit")
                    lower = bytes(body).lower()
                    result.update(
                        status=response.status_code,
                        server=response.headers.get("Server"),
                        content_type=response.headers.get("Content-Type"),
                        bytes=len(body),
                        sha256=hashlib.sha256(body).hexdigest(),
                        request_headers={
                            key: value for key, value in response.request.headers.items()
                            if key.lower() in {
                                "user-agent", "accept", "accept-encoding", "accept-language",
                                "connection", "referer",
                            }
                        },
                        has_cookie="Cookie" in response.request.headers,
                        has_authorization="Authorization" in response.request.headers,
                        challenge=any(word in lower for word in (
                            b"captcha", b"cf-chl-", b"verify you are human"
                        )),
                    )
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    # No further requests when challenged, throttled or asked to authenticate.
                    if result["challenge"] or response.status_code in {401, 429, 503}:
                        return
            except requests.RequestException as error:
                print(json.dumps({**result, "error": type(error).__name__}), flush=True)
                return

    time.sleep(6)
    with tempfile.TemporaryDirectory() as directory:
        body_path = Path(directory) / "body"
        header_path = Path(directory) / "headers"
        completed = subprocess.run(
            [
                "curl", "--silent", "--show-error", "--max-time", "20",
                "--max-filesize", "2097152", "--compressed",
                "--user-agent", PUBLIC_RESEARCH_USER_AGENT, "--header", f"Accept: {ACCEPT}",
                "--output", str(body_path), "--dump-header", str(header_path),
                "--write-out", "%{http_code} %{http_version}", URL,
            ], capture_output=True, text=True, check=False,
        )
        body = body_path.read_bytes() if body_path.exists() else b""
        headers = header_path.read_text() if header_path.exists() else ""
        safe_headers = [line for line in headers.splitlines() if line.lower().startswith(
            ("http/", "server:", "content-type:")
        )]
        print(json.dumps({
            "variant": "standard-curl", "completed_at": datetime.now(UTC).isoformat(),
            "returncode": completed.returncode, "status_protocol": completed.stdout,
            "headers": safe_headers, "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }), flush=True)


if __name__ == "__main__":
    main()
