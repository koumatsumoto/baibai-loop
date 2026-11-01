"""Report effective request configuration without sending any HTTP request."""
import hashlib
import json
import os
import platform
import ssl
from pathlib import Path
import requests
import urllib3

url = "https://minkabu.jp/stock/2204"
headers = {"User-Agent": "baibai-loop-public-research/1.0", "Accept": "text/html"}
with requests.Session() as session:
    prepared = session.prepare_request(requests.Request("GET", url, headers=headers))
    settings = session.merge_environment_settings(prepared.url, {}, False, None, None)
    verify = settings["verify"]
    ca_path = verify if isinstance(verify, str) else requests.certs.where()
    ca_hash = hashlib.sha256(Path(ca_path).read_bytes()).hexdigest() if Path(ca_path).is_file() else None
    safe = ("User-Agent", "Accept", "Accept-Encoding", "Connection", "If-None-Match", "If-Modified-Since")
    print(json.dumps({
        "provider_requests": 0,
        "python": platform.python_version(),
        "openssl": ssl.OPENSSL_VERSION,
        "requests": requests.__version__,
        "urllib3": urllib3.__version__,
        "os": platform.system(),
        "head": os.environ.get("GITHUB_SHA"),
        "prepared_url": prepared.url,
        "prepared_method": prepared.method,
        "headers": {k: prepared.headers[k] for k in safe if k in prepared.headers},
        "cookie_present": "Cookie" in prepared.headers,
        "authorization_present": "Authorization" in prepared.headers,
        "trust_env": session.trust_env,
        "configured_verify": session.verify,
        "effective_verify_kind": "path" if isinstance(verify, str) else str(verify),
        "ca_bundle_sha256": ca_hash,
        "effective_proxy_keys": sorted(settings["proxies"]),
        "effective_https_proxy": bool(requests.utils.select_proxy(prepared.url, settings["proxies"])),
        "environment_present": {k: bool(os.environ.get(k)) for k in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
            "all_proxy", "no_proxy", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")},
        "adapter_max_retries": session.adapters["https://"].max_retries.total,
        "stream": settings["stream"],
        "redirect_policy": "not invoked; production forbids redirects"
    }, sort_keys=True))
