"""Transport layer.

cdek.shopping sits behind a JS challenge: a plain GET answers 503 plus a
challenge script, and plain httpx stays blocked even with the solved-challenge
cookies -- the WAF also checks the TLS fingerprint. So:

  1. boot a real Chromium once, let it solve the challenge  (~10s)
  2. lift its cookies + user agent, then close the browser
  3. run the crawl on curl_cffi, which impersonates Chrome's TLS fingerprint

That is ~0.4-0.9s per page against ~2.4s through the browser. If step 3 ever
stops working the fetcher keeps the browser open and fetches from inside the
page instead: slower, but always available.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cr
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

CHALLENGE_MARKERS = ("js-challenge-script", "js-challenge-validation")
IMPERSONATE = "chrome131"


def _looks_real(body: str, expect_json: bool) -> bool:
    if expect_json:
        return body.lstrip().startswith(("{", "["))
    if len(body) < 5000:
        return False
    return not any(m in body for m in CHALLENGE_MARKERS)


class Fetcher:
    """Fetches pages as text, hiding the challenge dance from callers."""

    def __init__(self, base_url: str, timeout_s: int = 30, retries: int = 3, headless: bool = True):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self.headless = headless
        self.mode = "http"                 # "http" (curl_cffi) or "browser"
        self.cookies: dict[str, str] = {}
        self.user_agent = ""
        self._generation = 0               # bumped on refresh; invalidates sessions
        self._local = threading.local()    # one curl_cffi session per thread
        self._lock = threading.Lock()
        self._pw = self._browser = self._context = self._page = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        self._open_browser()
        self._solve_challenge()
        self._harvest_cookies()
        if self._http_works():
            self.mode = "http"
            self._close_browser()          # not needed until the cookies expire
        else:
            self.mode = "browser"
            log.info("curl_cffi rejected; staying in browser mode")
        log.info("transport ready: mode=%s", self.mode)

    def close(self) -> None:
        self._close_browser()

    # -- browser side ------------------------------------------------------
    def _open_browser(self) -> None:
        if self._page is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(locale="ru-RU", viewport={"width": 1440, "height": 900})
        self._page = self._context.new_page()

    def _close_browser(self) -> None:
        for obj in (self._context, self._browser):
            try:
                obj.close()
            except Exception:
                pass
        try:
            self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None

    def _solve_challenge(self) -> None:
        for attempt in range(1, self.retries + 2):
            self._page.goto(self.base_url + "/", wait_until="domcontentloaded", timeout=self.timeout_s * 1000)
            if _looks_real(self._page.content(), expect_json=False):
                return
            log.info("challenge in flight, retry %s", attempt)
            self._page.wait_for_timeout(2500)
        raise RuntimeError("could not get past the JS challenge")

    def _harvest_cookies(self) -> None:
        self.cookies = {c["name"]: c["value"] for c in self._context.cookies()}
        self.user_agent = self._page.evaluate("navigator.userAgent")

    # -- curl_cffi side ----------------------------------------------------
    def _session(self) -> cr.Session:
        """A curl_cffi session bound to this thread and to the cookie generation."""
        session = getattr(self._local, "session", None)
        if session is None or getattr(self._local, "generation", -1) != self._generation:
            session = cr.Session(
                impersonate=IMPERSONATE,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": self.base_url + "/",
                },
            )
            for name, value in self.cookies.items():
                session.cookies.set(name, value, domain=".cdek.shopping")
            self._local.session = session
            self._local.generation = self._generation
        return session

    def _http_works(self) -> bool:
        try:
            r = self._session().get(self.base_url + "/c/2685/krossovki?is_sale=1", timeout=self.timeout_s)
            return r.status_code == 200 and _looks_real(r.text, False)
        except Exception as exc:
            log.info("curl_cffi probe failed: %s", exc)
            return False

    def _refresh(self, generation_seen: int) -> None:
        """Cookies went stale: re-solve the challenge and re-seed sessions.

        generation_seen keeps every worker thread from re-solving in turn:
        whoever refreshed first wins, the rest just pick up the new cookies.
        """
        with self._lock:
            if generation_seen != self._generation:
                return
            log.info("refreshing session")
            self._open_browser()
            self._solve_challenge()
            self._harvest_cookies()
            self._generation += 1
            if self.mode == "http":
                self._close_browser()

    # -- fetching ----------------------------------------------------------
    def get(self, path: str, headers: dict | None = None, expect_json: bool = False) -> str:
        """Fetch a site-relative path and return its body."""
        expect_json = expect_json or bool(headers and headers.get("X-Requested-With"))
        last_error = None
        for attempt in range(1, self.retries + 1):
            generation = self._generation
            try:
                body = self._get_once(path, headers)
                if _looks_real(body, expect_json):
                    return body
                # Only a challenge means the session died. A wrong-shaped answer
                # (JSON expected, HTML returned) is a request problem, and
                # re-solving the challenge would not fix it.
                if any(m in body[:20000] for m in CHALLENGE_MARKERS):
                    last_error = "challenge returned"
                    self._refresh(generation)
                else:
                    raise RuntimeError(f"unexpected response ({len(body)} bytes)")
            except Exception as exc:
                last_error = repr(exc)
                log.warning("fetch %s failed (%s/%s): %s", path, attempt, self.retries, exc)
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"fetch failed for {path}: {last_error}")

    def get_many(self, paths: list[str], workers: int = 4) -> list[str | None]:
        """Fetch several paths in parallel, preserving order. None marks a failure."""
        if not paths:
            return []
        if self.mode == "browser":
            return self._browser_batch(paths)

        def job(path):
            try:
                return self.get(path)
            except Exception as exc:
                log.warning("giving up on %s: %s", path, exc)
                return None

        with ThreadPoolExecutor(min(workers, len(paths))) as pool:
            return list(pool.map(job, paths))

    def _get_once(self, path: str, headers: dict | None = None) -> str:
        if self.mode == "http":
            r = self._session().get(self.base_url + path, headers=headers, timeout=self.timeout_s)
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            return r.text
        # Browser mode: fetch from inside the page so cookies/headers are native.
        self._open_browser()
        return self._page.evaluate(
            """async ({p, h}) => {
                const r = await fetch(p, {credentials: 'include', headers: h || {}});
                return await r.text();
            }""",
            {"p": path, "h": headers or {}},
        )

    def _browser_batch(self, paths: list[str]) -> list[str | None]:
        self._open_browser()
        return self._page.evaluate(
            """async (ps) => Promise.all(ps.map(p =>
                fetch(p, {credentials: 'include'}).then(r => r.text()).catch(() => null)))""",
            paths,
        )
