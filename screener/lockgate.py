"""The module-lock gate: what the server answers while VICARIUS has Sponge Screener locked.

The request handler asks ``refusal`` before it routes a request. While the
module is unlocked, or for a static file or the health check, the answer is
None and the request goes on. A person opening a page gets the lock page;
every other call gets HTTP 423 with the lock message.
"""

import json
from dataclasses import dataclass
from typing import Optional

from screener import lock_status

STATIC_PREFIX = "/static/"
# A page under /static/ (index.html) is the app itself, so the lock covers it.
PAGE_SUFFIX = ".html"
HEALTH_PATH = "/api/health"
# Paths a person never navigates to: the API, the images, the guide, and the video relay.
NON_PAGE_PREFIXES = ("/api/", "/media/", "/guide/", "/video")
PAGE_METHODS = ("GET",)
PAGE_TYPES = ("text/html", "application/xhtml+xml", "*/*")

HTTP_OK = 200
HTTP_LOCKED = 423
HTML_TYPE = "text/html; charset=utf-8"
JSON_TYPE = "application/json; charset=utf-8"

# Inline styles only: while the module is locked the page cannot rely on any
# other request succeeding.
LOCK_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sponge Screener (locked)</title>
<style>
  html, body { margin: 0; height: 100%; }
  body { background: #0a0a0f; color: #F5EDF0; font-family: system-ui, -apple-system, sans-serif;
         display: flex; align-items: center; justify-content: center; }
  .vic-lock-page { text-align: center; padding: 40px 16px; max-width: 520px; }
  .vic-lock-page svg { display: block; margin: 0 auto 18px; }
  .vic-lock-page h1 { font-size: 22px; font-weight: 600; margin: 0 0 12px; }
  .vic-lock-page p { font-size: 15px; line-height: 1.6; margin: 0; color: #b0a7ab; }
  .vic-lock-page a { color: #FFB3D9; }
  .vic-lock-footer { margin-top: 28px; font-size: 12px; color: #b0a7ab; }
  .vic-lock-footer + .vic-lock-footer { margin-top: 4px; }
</style>
</head>
<body>
<div class="vic-lock-page">
  <svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 24 24" fill="none"
       stroke="#b0a7ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" role="img"
       aria-label="Locked">
    <rect x="4" y="10.5" width="16" height="10" rx="2"></rect>
    <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"></path>
    <circle cx="12" cy="15.5" r="1.4" fill="#b0a7ab" stroke="none"></circle>
  </svg>
  <h1>Sponge Screener</h1>
  <p>Under development. Please come back later.
     <a href="mailto:lauren.olinger@uvi.edu">Email Lauren</a> for questions.</p>
  <div class="vic-lock-footer">VICARIUS module lock</div>
  <div class="vic-lock-footer">Built by Lauren Olinger</div>
</div>
</body>
</html>
"""


@dataclass(frozen=True)
class Refusal:
    """The answer the server sends in place of the route while the module is locked.

    Attributes:
        status: 200 for the lock page, 423 for every other call.
        content_type: The Content-Type header value.
        body: The encoded body.
    """

    status: int
    content_type: str
    body: bytes


def is_page_request(method: str, path: str, accept: Optional[str]) -> bool:
    """Say whether a request is a person opening a page rather than the page calling the server.

    Args:
        method: The HTTP method.
        path: The percent-decoded path, without the query string.
        accept: The Accept header, or None when the request sent none.

    Returns:
        True for a GET outside the API, image, guide and video paths whose
        Accept header is missing or names HTML or any type.
    """
    if method not in PAGE_METHODS:
        return False
    if path.startswith(NON_PAGE_PREFIXES):
        return False
    if not accept:
        return True
    return any(kind in accept for kind in PAGE_TYPES)


def refusal(method: str, path: str, accept: Optional[str]) -> Optional[Refusal]:
    """Decide what a locked module answers, reading the lock registry fresh.

    Args:
        method: The HTTP method.
        path: The percent-decoded path, without the query string.
        accept: The Accept header, or None.

    Returns:
        None when the module is unlocked, or the request is for a static file
        other than an HTML page, or the health check. Otherwise the lock page
        for a page request, or a 423 JSON answer
        ``{"error": "locked: <message>", "locked": true}``.
    """
    if not lock_status.is_locked():
        return None
    if path == HEALTH_PATH:
        return None
    if path.startswith(STATIC_PREFIX) and not path.lower().endswith(PAGE_SUFFIX):
        return None
    if is_page_request(method, path, accept):
        return Refusal(HTTP_OK, HTML_TYPE, LOCK_PAGE_HTML.encode("utf-8"))
    payload = {"error": f"locked: {lock_status.LOCK_MESSAGE}", "locked": True}
    return Refusal(HTTP_LOCKED, JSON_TYPE, json.dumps(payload).encode("utf-8"))
