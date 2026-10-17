"""StepStone transport: curl_cffi sessions on sticky residential exits in the
board's own country, with a headful patchright browser as the last resort.

Every StepStone Group board (stepstone/sites.py) sits behind Akamai Bot
Manager, which refuses datacenter egress outright (HTTP 403 "Access Denied"
from the edge, reference #18…, for the GKE pod, this Mac and every DC proxy
tried on 2026-10-15/17) but serves a Chrome-impersonated curl_cffi session
on a residential proxy exit of the board's country every time: search
pages, job pages, company pages and the autosuggest JSON. So each call
leaves through config.stepstone_proxy(<country>) — a fresh sticky exit per
session, sessions pooled per country and retired after
config.STEPSTONE_REQUESTS_PER_SESSION calls. A blocked call is retried on a
fresh exit up to config.STEPSTONE_MAX_ATTEMPTS times; when every attempt was
refused one headful patchright Chrome on the same kind of exit loads the page
(config.STEPSTONE_BROWSER_FALLBACK) and stays open for
config.STEPSTONE_BROWSER_IDLE_SECONDS.

Surfaces:
  page(site, path, params)      GET a board page -> Response(text, url, status)
  get_json(site, path, params)  GET a JSON endpoint (autosuggest)
  site_id(site)                 the board's siteId (registry, else read from a search page)

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  StepstoneUpstreamError  transport failure / 5xx / unexpected body — retryable
  StepstoneBlocked        Akamai "Access Denied", the site's own "Error - Access
                          denied" page, 429                         — retryable
  StepstoneBadRequest     upstream rejected the params              — never retried
  StepstoneNotFound       the page does not exist                   — never retried

Smoke test:  python stepstone/fetch.py [keyword] [site]
"""
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from stepstone import sites as S

TIMEOUT = (10, 90)          # (connect, read) — result pages are 1-1.4 MB of HTML

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "upgrade-insecure-requests": "1",
}
JSON_HEADERS = {"accept": "application/json, text/plain, */*"}

BLOCK_STATUSES = (401, 403, 429, 503)
# Akamai's edge page (tiny, "Reference #18.…") and the site's own error shell
DENIED_MARKERS = ("<TITLE>Access Denied</TITLE>", "errors.edgesuite.net", "<title>Error - Access denied</title>")
NOT_FOUND_MARKERS = ("<title>Error - Page not found</title>",)
STATE_MARKER = "window.__PRELOADED_STATE__"
BROWSER_SETTLE_TIMEOUT = 45
RATE_LIMIT_BACKOFF = 2.0
_SITE_ID_RE = re.compile(r'"siteId":\s*(\d+)')


class StepstoneUpstreamError(UpstreamError):
    """Transport failure, 5xx or an unexpected body — retryable."""


class StepstoneBlocked(StepstoneUpstreamError, Blocked):
    """Akamai refused the exit (403), the site's access-denied shell, or 429."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class StepstoneBadRequest(BadRequest):
    """StepStone answered 400 to params it does not accept."""


class StepstoneNotFound(NotFound):
    """The job / company / page does not exist — never retried."""


class Response:
    __slots__ = ("text", "url", "status")

    def __init__(self, text, url, status):
        self.text = text or ""
        self.url = url or ""
        self.status = status


def dump_debug(name, text):
    """Write a raw response to $STEPSTONE_DEBUG_DIR/<name>.txt."""
    dbg = os.environ.get("STEPSTONE_DEBUG_DIR", "")
    if dbg and text:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name + ".txt"), "w") as f:
                f.write(text if isinstance(text, str) else json.dumps(text))
        except OSError:
            pass


# ---- sessions (pooled per proxy country) ---------------------------------------------------
# A curl_cffi Session is not thread-safe, so a session is checked OUT for the
# duration of one call and returned afterwards.

class _Session:
    __slots__ = ("sess", "used", "proxy", "country", "impersonate", "last_headers")

    def __init__(self, sess, proxy, country, impersonate):
        self.sess = sess
        self.proxy = proxy
        self.country = country
        self.impersonate = impersonate
        self.used = 0
        self.last_headers = PAGE_HEADERS

    def close(self):
        try:
            self.sess.close()
        except Exception:
            pass


_idle = {}                  # country -> [sessions]
_idle_lock = threading.Lock()
_fresh_count = 0


def _budget():
    return max(1, int(config.STEPSTONE_REQUESTS_PER_SESSION))


def _new_session(country):
    """A new curl session: next TLS identity in rotation, on a fresh sticky
    exit of `country`, with an empty cookie jar."""
    global _fresh_count
    from curl_cffi import requests as curl_requests
    identities = tuple(config.STEPSTONE_IMPERSONATIONS) or ("chrome",)
    with _idle_lock:
        impersonate = identities[_fresh_count % len(identities)]
        _fresh_count += 1
    proxy = config.stepstone_proxy(country)
    sess = curl_requests.Session(impersonate=impersonate)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    return _Session(sess, proxy, country, impersonate)


def _checkout(country, fresh=False):
    if not fresh:
        with _idle_lock:
            pool = _idle.get(country) or []
            if pool:
                return pool.pop()
    return _new_session(country)


def _checkin(session):
    session.used += 1
    if session.used >= _budget():
        session.close()
        return
    with _idle_lock:
        _idle.setdefault(session.country, []).append(session)


def reset_sessions():
    """Drop every idle session (tests, or after a burst of blocks)."""
    with _idle_lock:
        sessions = [s for pool in _idle.values() for s in pool]
        _idle.clear()
    for session in sessions:
        session.close()


# ---- classification -----------------------------------------------------------------------

def classify(status, text, label, final_url=None):
    """Raise the failure a response represents, or return it as a Response."""
    text = text or ""
    head = text[:30000]
    if status in BLOCK_STATUSES or any(m in head for m in DENIED_MARKERS):
        dump_debug("blocked", text[:5000])
        raise StepstoneBlocked(f"access denied (HTTP {status}) on {label}", status=status)
    if status in (404, 410) or any(m in head for m in NOT_FOUND_MARKERS):
        raise StepstoneNotFound(f"StepStone has nothing at {label}")
    if status == 400:
        dump_debug("bad_request", text[:5000])
        raise StepstoneBadRequest(f"StepStone rejected {label}")
    if status != 200:
        raise StepstoneUpstreamError(f"HTTP {status} on {label}")
    return Response(text, final_url, status)


# ---- browser fallback -----------------------------------------------------------------------

class _Browser:
    """One headful patchright Chrome on a residential exit of the board's
    country, opened on demand, reused while busy and closed after
    config.STEPSTONE_BROWSER_IDLE_SECONDS unused. A request for another
    country replaces it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.driver = None
        self.country = None
        self.last_used = 0.0
        self.closer = None

    def _open(self, country):
        from chrome_manager import create_scope
        from patchright_driver import PatchrightDriver
        with create_scope():
            driver = PatchrightDriver(proxy_url=config.stepstone_proxy(country), headless=False, geoip_timezone=True)
        return driver

    @staticmethod
    def _nav(driver, url):
        """Navigate and wait for a real StepStone page (one carrying the preloaded state)."""
        result = driver.nav(url, referer="https://www.google.com/", mode="blocked")
        html = (result or {}).get("html") or ""
        status = (result or {}).get("status") or 200
        deadline = time.monotonic() + BROWSER_SETTLE_TIMEOUT
        while STATE_MARKER not in html:
            if any(m in html[:30000] for m in NOT_FOUND_MARKERS) or status in (404, 410):
                break
            if time.monotonic() > deadline:
                raise StepstoneBlocked("browser never reached a StepStone page")
            time.sleep(1.5)
            try:
                html = driver.content() or ""
            except Exception:
                html = ""
        return html, status, (result or {}).get("url") or url

    def _close_if_idle(self):
        while True:
            time.sleep(30)
            with self.lock:
                if self.driver is None:
                    self.closer = None
                    return
                if time.time() - self.last_used > config.STEPSTONE_BROWSER_IDLE_SECONDS:
                    self._drop()
                    self.closer = None
                    return

    def _drop(self):
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                pass
        self.driver = None
        self.country = None

    def _ensure(self, country):
        if self.driver is not None and self.country != country:
            self._drop()
        if self.driver is None:
            self.driver = self._open(country)
            self.country = country
        if self.closer is None:
            self.closer = threading.Thread(target=self._close_if_idle, daemon=True)
            self.closer.start()
        self.last_used = time.time()

    def page(self, country, url, label):
        with self.lock:
            self._ensure(country)
            try:
                html, status, final_url = self._nav(self.driver, url)
            except StepstoneNotFound:
                raise
            except Exception:
                self._drop()
                raise
        return classify(status, html, label, final_url)


_browser = _Browser()


# ---- requests -------------------------------------------------------------------------------------

def _url(site_row, path, params):
    """The absolute URL of `path` with `params` appended; a path that already
    carries a query (the company pages: /jobs?cmpId=<id>&cmp=1) gets them
    joined with '&', never a second '?'."""
    query = {k: v for k, v in (params or {}).items() if v not in (None, "", False, [])}
    url = S.link(site_row, path)
    if not query:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(query, doseq=True)


_INTERSTITIAL_MARKER = "/_sec/verify?provider=interstitial"
_POW_RE = re.compile(r'var i = (\d+); var j = i \+ Number\("(\d+)" \+ "(\d+)"\);')
_VERIFY_TOKEN_RE = re.compile(r'JSON\.stringify\(\{"bm-verify":\s*"([^"]+)"')


def _solve_interstitial(session, url, text, label):
    """Akamai's interstitial page (a 2-3 KB shell with a proof-of-work the
    browser would POST to /_sec/verify, then reload): compute the answer,
    POST it on the same session so the bm_sz / _abck cookies get cleared,
    and return the reloaded page. Seen on company pages reached through
    ?cmpId= (2026-10-17)."""
    pow_match = _POW_RE.search(text)
    token_match = _VERIFY_TOKEN_RE.search(text)
    if not pow_match or not token_match:
        raise StepstoneBlocked(f"unsolvable Akamai interstitial on {label}", status=403)
    answer = int(pow_match.group(1)) + int(pow_match.group(2) + pow_match.group(3))
    origin = "https://" + (urlsplit(url).hostname or "")
    try:
        verify = session.sess.post(origin + _INTERSTITIAL_MARKER, headers={
            "accept": "*/*", "content-type": "application/json", "origin": origin, "referer": url},
            data=json.dumps({"bm-verify": token_match.group(1), "pow": answer}), timeout=TIMEOUT)
        resp = session.sess.get(url, headers=session.last_headers, timeout=TIMEOUT, allow_redirects=True)
    except Exception as e:
        raise StepstoneUpstreamError(f"interstitial verify failed on {label}: {type(e).__name__}: {e}")
    if _INTERSTITIAL_MARKER in resp.text[:5000]:
        dump_debug("interstitial", verify.text[:2000] + "\n----\n" + resp.text[:3000])
        raise StepstoneBlocked(f"Akamai interstitial not cleared on {label} (verify HTTP {verify.status_code})", status=403)
    print(f"stepstone: cleared an Akamai interstitial on {label}")
    return resp


def _curl_get(session, url, headers, label):
    session.last_headers = headers
    try:
        resp = session.sess.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) < 10000 and _INTERSTITIAL_MARKER in resp.text:
            resp = _solve_interstitial(session, str(resp.url), resp.text, label)
    except (StepstoneBlocked, StepstoneUpstreamError):
        raise
    except Exception as e:
        raise StepstoneUpstreamError(f"request failed on {label}: {type(e).__name__}: {e}")
    return classify(resp.status_code, resp.text, label, str(resp.url))


def _with_sessions(country, curl_call, browser_call, label):
    """Run curl_call(session) on pooled sessions of `country` (fresh ones after
    a failure), then browser_call() when every attempt was refused."""
    last = None
    blocked_only = True
    attempts = max(1, int(config.STEPSTONE_MAX_ATTEMPTS))
    for attempt in range(1, attempts + 1):
        session = None
        rate_limited = False
        try:
            session = _checkout(country, fresh=attempt > 1)
            out = curl_call(session)
            _checkin(session)
            return out
        except StepstoneBlocked as e:
            last = e
            rate_limited = e.status == 429
            if session is not None:
                session.close()
        except (StepstoneNotFound, StepstoneBadRequest):
            if session is not None:
                _checkin(session)
            raise
        except StepstoneUpstreamError as e:
            last, blocked_only = e, False
            if session is not None:
                session.close()
        except Exception as e:
            last, blocked_only = StepstoneUpstreamError(f"{label}: {type(e).__name__}: {e}"), False
            if session is not None:
                session.close()
        print(f"stepstone: attempt {attempt}/{attempts} on {label} failed: {last}")
        if attempt < attempts:
            time.sleep(RATE_LIMIT_BACKOFF * attempt if rate_limited else 0.4 * attempt)
    if blocked_only and config.STEPSTONE_BROWSER_FALLBACK and browser_call is not None:
        print(f"stepstone: every curl attempt on {label} was refused; trying the browser")
        try:
            return browser_call()
        except (StepstoneNotFound, StepstoneBadRequest):
            raise
        except Exception as e:
            print(f"stepstone: browser fallback failed on {label}: {type(e).__name__}: {e}")
            raise StepstoneBlocked(f"{label}: refused on {attempts} exits and in a browser") from e
    raise last


def page(site, path, params=None, label=None):
    """GET a board page -> Response(text, final url, status). `site` is a
    site code or a sites.site() row."""
    row = S.site(site) if isinstance(site, str) else site
    url = _url(row, path, params)
    label = label or f"{row['host']}{path}"
    headers = {**PAGE_HEADERS, "accept-language": row["accept_language"]}
    country = row["proxy_country"]
    return _with_sessions(country, lambda s: _curl_get(s, url, headers, label),
                          lambda: _browser.page(country, url, label), label)


def get_json(site, path, params=None, label=None):
    """GET a board JSON endpoint -> parsed body."""
    row = S.site(site) if isinstance(site, str) else site
    url = _url(row, path, params)
    label = label or f"{row['host']}{path}"
    headers = {**JSON_HEADERS, "accept-language": row["accept_language"], "referer": S.link(row, "/")}
    resp = _with_sessions(row["proxy_country"], lambda s: _curl_get(s, url, headers, label), None, label)
    try:
        return json.loads(resp.text) if resp.text.strip() else None
    except ValueError:
        dump_debug("non_json", resp.text)
        raise StepstoneUpstreamError(f"non-JSON body on {label}")


# ---- memo + fan-out ---------------------------------------------------------------------------------

_memo = OrderedDict()
_memo_lock = threading.Lock()
MEMO_TTL = 24 * 3600
MEMO_MAX = 256


def memoized(key, fn, ttl=MEMO_TTL):
    now = time.monotonic()
    with _memo_lock:
        hit = _memo.get(key)
        if hit and now - hit[0] < ttl:
            _memo.move_to_end(key)
            return hit[1]
    value = fn()
    with _memo_lock:
        _memo[key] = (now, value)
        _memo.move_to_end(key)
        while len(_memo) > MEMO_MAX:
            _memo.popitem(last=False)
    return value


def site_id(site):
    """The board's siteId for the autosuggest API: from the registry, else
    read once from a search page's result-list config and memoised."""
    row = S.site(site) if isinstance(site, str) else site
    if row.get("site_id"):
        return row["site_id"]

    def discover():
        resp = page(row, row["search_prefix"] + "/python", label=f"{row['host']} siteId discovery")
        start = resp.text.find('"app-unifiedResultlist"')
        match = _SITE_ID_RE.search(resp.text, start if start >= 0 else 0)
        if not match:
            raise StepstoneUpstreamError(f"no siteId in a {row['host']} search page")
        return int(match.group(1))
    return memoized(("site_id", row["code"]), discover)


def run_parallel(fns, workers=None):
    """Run zero-arg callables in parallel; results align with `fns`. Each
    result is ("ok", value) or ("error", exception)."""
    def guard(fn):
        try:
            return ("ok", fn())
        except Exception as e:
            return ("error", e)
    if not fns:
        return []
    workers = workers or int(config.STEPSTONE_WORKERS)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(fns)))) as ex:
        return list(ex.map(guard, fns))


if __name__ == "__main__":
    from stepstone import parsers
    term = sys.argv[1] if len(sys.argv) > 1 else "python"
    code = sys.argv[2] if len(sys.argv) > 2 else "de"
    row = S.site(code)
    resp = page(row, S.search_path(row, term))
    state = parsers.extract_app(resp.text, "app-unifiedResultlist")
    out = parsers.search_results(state, row)
    print(out["pagination"], [j["title"] for j in out["jobs"][:5]])
