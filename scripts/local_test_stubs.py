#!/usr/bin/env python3
"""A canned stand-in for Tavily and Moltbook, for the `localtest` Compose
profile (see docs/operations.md: "Local testing against a small model").

Exists so the local smoke test can exercise a real (small) model against
the real acquisition code path -- building a request, parsing a response,
turning it into grounded evidence -- without ever making a real paid
Tavily call or a real public Moltbook one. Stdlib only, no project
dependency: it runs in a bare `python` image, not the project's own, so it
stays usable even if the project's dependencies change.

Routes match what `collegium.acquisition.tavily` and `.moltbook` call:
  POST /search                        Tavily discover()
  POST /extract                       Tavily extract()
  GET  /api/v1/search                 Moltbook discover()
  GET  /api/v1/posts/<id>             Moltbook _post()
  GET  /api/v1/posts/<id>/comments    Moltbook replies()
"""

import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = 8080


def _tavily_result(n: int) -> dict:
    return {
        "url": f"http://stub.invalid/article-{n}",
        "title": f"Stub article {n}",
        "content": f"This is canned local-test content for article {n}. "
        "It exists only to give a local model something plausible to read.",
        # Always "today": the Scout drops results outside its recency
        # window, so a fixed past date would quietly make every search
        # come back empty.
        "published_date": datetime.now(UTC).date().isoformat(),
        "score": 0.9,
    }


class Handler(BaseHTTPRequestHandler):
    def _reply(self, body: dict, status: int = 200) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler's naming)
        path = urlsplit(self.path).path
        if path == "/search":
            self._reply({"results": [_tavily_result(1), _tavily_result(2)]})
        elif path == "/extract":
            urls = self._read_json().get("urls", [])
            self._reply(
                {
                    "results": [
                        {"url": u, "raw_content": f"Canned local-test content for {u}."}
                        for u in urls
                    ]
                }
            )
        else:
            self._reply({"error": "no such stub route"}, status=404)

    def do_GET(self):  # noqa: N802
        split = urlsplit(self.path)
        path = split.path
        if path == "/api/v1/search":
            qs = parse_qs(split.query)
            if qs.get("type", ["posts"])[0] != "posts":
                self._reply({"results": []})
                return
            self._reply({"results": [{"type": "post", "post_id": "stub-post-1"}]})
        elif path.startswith("/api/v1/posts/") and path.endswith("/comments"):
            self._reply({"comments": []})
        elif path.startswith("/api/v1/posts/"):
            self._reply(
                {
                    "post": {
                        "id": path.rsplit("/", 1)[-1],
                        "title": "A stubbed Moltbook post",
                        "content": "Canned local-test content standing in for a real post.",
                        "author": {"name": "stub-agent"},
                        "submolt": {"name": "test"},
                        "upvotes": 1,
                        "comment_count": 0,
                        "created_at": datetime.now(UTC).isoformat(),
                        "is_spam": False,
                        "is_deleted": False,
                    }
                }
            )
        else:
            self._reply({"error": "no such stub route"}, status=404)

    def log_message(self, fmt, *args):  # quieter than the default
        print(f"stub: {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
