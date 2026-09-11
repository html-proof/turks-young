"""Read-only public artwork probe. Run from repository root with python -m scripts.validate_artwork.

Uses HEAD (no full-size downloads), bounded concurrency and redirects.
Never prints signed query strings, user tokens or audio URLs.
Private home/history require an authenticated device and are explicitly excluded.
"""
import concurrent.futures
import json
import time
import subprocess
import os
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

BASE = "https://music-hub-web.imeseban.workers.dev"
# Read-only probe inputs for this developer utility. These are never loaded
# by the application or its recommendation/home APIs.
ARTWORK_VALIDATION_TARGETS = [
    ("featured", "Theekkali Aarambham", "track"),
    ("featured", "Kanmaniye", "track"),
    ("recommended", "Varnajaalam", "track"),
    ("recommended", "Murivukal", "track"),
    ("search", "Thalapathy Kacheri", "track"),
    ("album", "Annan Thampi", "album"),
    ("artist", "Armaan Malik", "artist"),
    ("playlist", "Malayalam", "playlist"),
]


class Redirects(HTTPRedirectHandler):
    max_redirections = 5
    def __init__(self):
        self.count = 0

    def redirect_request(self, *args, **kwargs):
        self.count += 1
        return super().redirect_request(*args, **kwargs)


def probe(sample):
    section, query, kind = sample
    result = {"section": section, "query": query, "items": []}
    try:
        endpoint = BASE + "/api/search?" + urlencode({"q": query, "type": kind, "limit": 3})
        metadata = subprocess.run(["curl", "--silent", "--show-error", "--fail",
            "--max-time", "25", endpoint], capture_output=True, text=True, check=True)
        payload = json.loads(metadata.stdout).get("data", {})
        for item in payload.get("items", []):
            url = item.get("image_url") or item.get("artworkUrl") or ""
            row = {"id": item.get("id"), "title": item.get("title") or item.get("name"),
                   "host": urlsplit(url).hostname, "status": "missing_metadata"}
            if url:
                redirects = Redirects()
                started = time.monotonic()
                try:
                    check = subprocess.run(["curl", "--silent", "--show-error", "--head",
                        "--location", "--max-redirs", "5", "--max-time", "10",
                        "--output", os.devnull, "--write-out", "%{json}", url],
                        capture_output=True, text=True)
                    info = json.loads(check.stdout)
                    content_type = info.get("content_type") or ""
                    code = info.get("http_code", 0)
                    redirects.count = info.get("num_redirects", 0)
                    row.update(http=code, content_type=content_type,
                        status=("transport_error" if check.returncode else
                            "http_error" if code >= 400 else
                            "valid_headers" if content_type.startswith("image/") else "invalid_content_type"))
                except HTTPError as error:
                    row.update(status="http_error", http=error.code)
                except Exception as error:
                    row.update(status=type(error).__name__)
                row.update(redirects=redirects.count, elapsed_ms=round((time.monotonic()-started)*1000))
                if "default-album" in url or "placeholder" in url:
                    row["status"] = "provider_placeholder"
            result["items"].append(row)
    except Exception as error:
        result["error"] = type(error).__name__
    return result


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        samples = list(executor.map(probe, ARTWORK_VALIDATION_TARGETS))
    totals = {}
    for sample in samples:
        for item in sample["items"]:
            totals[item["status"]] = totals.get(item["status"], 0) + 1
    print(json.dumps({"totals": totals, "samples": samples,
          "limitations": "HEAD checks headers, not decode; public search proxies screenshot titles. Authenticated home/history and device offline/lockscreen untested."}, indent=2))
