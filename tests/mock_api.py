"""A local stand-in for SerpApi / Hunter.io / Google Places.

Replays real vendor response shapes so the integration code can be verified
end-to-end without paid API keys. Used by ``tests/test_api.py``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VALID_KEY = "GOOD_KEY"

# --------------------------------------------------------------------------- #
# Canned payloads, matching the real APIs field-for-field.
# --------------------------------------------------------------------------- #
SERPAPI_OK = {
    "search_metadata": {"id": "abc", "status": "Success"},
    "search_parameters": {"q": "dentists"},
    "organic_results": [
        {"position": 1, "title": "Smile Dental", "link": "https://smiledental.example.com/",
         "snippet": "Family dentistry"},
        {"position": 2, "title": "Bright Teeth", "link": "https://brightteeth.example.org/about",
         "cached_page_link": "https://webcache.googleusercontent.com/search?q=cache:x"},
        {"position": 3, "title": "Skip me", "link": "https://facebook.com/somepage"},
    ],
    "local_results": {
        "places": [{"title": "City Dental", "website": "https://citydental.example.net/"}]
    },
}

SERPAPI_PROCESSING = {
    "search_metadata": {
        "id": "pending", "status": "Processing",
        "json_endpoint": "http://HOST/serpapi_done",
    }
}

SERPAPI_ERROR = {"error": "Invalid API key. Your API key should be here: "
                          "https://serpapi.com/manage-api-key"}

HUNTER_OK = {
    "data": {
        "domain": "acme.com",
        "organization": "Acme Corporation",
        "country": "US",
        "industry": "Software",
        "twitter": "acmecorp",
        "facebook": "acmecorp",
        "linkedin": "https://www.linkedin.com/company/acme",
        "instagram": None,
        "emails": [
            {"value": "jane.doe@acme.com", "type": "personal", "confidence": 95,
             "first_name": "Jane", "last_name": "Doe", "position": "CTO",
             "department": "executive", "verification": {"status": "valid"}},
            {"value": "info@acme.com", "type": "generic", "confidence": 72,
             "first_name": None, "last_name": None, "position": None,
             "department": "support", "verification": {"status": "accept_all"}},
        ],
    },
    "meta": {"results": 2},
}

HUNTER_ERROR = {"errors": [{"id": "authentication_failed", "code": 401,
                            "details": "No user found for the API key supplied"}]}

PLACES_OK = {
    "status": "OK",
    "next_page_token": "TOKEN_PAGE_2",
    "results": [
        {
            "place_id": "PLACE1", "name": "Corner Cafe",
            "formatted_address": "12 Mall Rd, Lahore, Pakistan",
            "geometry": {"location": {"lat": 31.5497, "lng": 74.3436}},
            "rating": 4.5, "user_ratings_total": 210,
            "types": ["cafe", "food", "point_of_interest"],
            "business_status": "OPERATIONAL",
        }
    ],
}

PLACES_PAGE2 = {
    "status": "OK",
    "results": [
        {
            "place_id": "PLACE2", "name": "Second Cup",
            "formatted_address": "5 Main St, Lahore",
            "geometry": {"location": {"lat": 31.5, "lng": 74.3}},
            "rating": 4.1, "user_ratings_total": 66,
            "types": ["cafe"], "business_status": "OPERATIONAL",
        }
    ],
}

PLACES_DETAILS = {
    "PLACE1": {"status": "OK", "result": {
        "website": "https://cornercafe-test.com/",
        "formatted_phone_number": "042 111 222 333",
        "international_phone_number": "+92 42 111 222 333"}},
    "PLACE2": {"status": "OK", "result": {
        "website": "https://secondcup-test.org/",
        "formatted_phone_number": "042 999 888 777"}},
}

PLACES_DENIED = {"status": "REQUEST_DENIED",
                 "error_message": "The provided API key is invalid.",
                 "results": [], "html_attributions": []}

# A tiny site served locally so the crawl stage has something real to parse.
SITE_HTML = """<!doctype html><html><head><title>Corner Cafe</title>
<meta name="description" content="Best coffee in town">
</head><body>
<a href="mailto:hello@cornercafe-test.com">Email us</a>
<p>Call <a href="tel:+92 42 111 222 333">us</a></p>
<a href="https://www.instagram.com/cornercafe">Instagram</a>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def _send(self, payload, status=200):
        body = (payload if isinstance(payload, bytes)
                else json.dumps(payload).encode())
        ctype = "text/html" if isinstance(payload, bytes) else "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        host = f"{self.headers.get('Host')}"

        # ---- SerpApi ---------------------------------------------------- #
        if path == "/search.json":
            if q.get("api_key") != VALID_KEY:
                return self._send(SERPAPI_ERROR, 401)
            if q.get("q") == "__processing__":
                p = json.loads(json.dumps(SERPAPI_PROCESSING).replace("HOST", host))
                return self._send(p)
            if q.get("q") == "__empty__":
                return self._send({"search_metadata": {"status": "Success"},
                                   "organic_results": []})
            return self._send(SERPAPI_OK)
        if path == "/serpapi_done":
            return self._send(SERPAPI_OK)

        # ---- Hunter.io -------------------------------------------------- #
        if path == "/v2/domain-search":
            if q.get("api_key") != VALID_KEY:
                return self._send(HUNTER_ERROR, 401)
            if q.get("domain") == "empty.com":
                return self._send({"data": {"domain": "empty.com", "emails": []},
                                   "meta": {"results": 0}})
            return self._send(HUNTER_OK)

        # ---- Google Places ---------------------------------------------- #
        if path == "/maps/api/place/textsearch/json":
            if q.get("key") != VALID_KEY:
                return self._send(PLACES_DENIED)          # note: HTTP 200!
            if q.get("pagetoken"):
                return self._send(PLACES_PAGE2)
            if "zero" in (q.get("query") or ""):
                return self._send({"status": "ZERO_RESULTS", "results": []})
            return self._send(PLACES_OK)
        if path == "/maps/api/place/details/json":
            if q.get("key") != VALID_KEY:
                return self._send(PLACES_DENIED)
            return self._send(PLACES_DETAILS.get(q.get("place_id"), {"status": "NOT_FOUND"}))

        # ---- crawlable site + robots ------------------------------------ #
        if path == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n")
        if path.startswith("/site"):
            return self._send(SITE_HTML.encode())

        self.send_error(404)


class MockAPI:
    """Context manager that runs the mock server on a free port."""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MockAPI":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
