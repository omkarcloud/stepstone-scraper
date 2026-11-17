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
refused a headed Chrome on the browser box, on the same kind of exit, loads
the page (config.STEPSTONE_BROWSER_FALLBACK; recipes/stepstone.py).

Since 2026-11-11 a call tries the gateway exit of the board's country first
(config.STEPSTONE_GATEWAY_EXITS: "gateway:de" for stepstone.de and, having no
gateway of their own, .at / .be / .nl / pnet; "gateway:gb" for the UK boards),
once, then the residential attempts above unchanged. The boards take the
client from X-Forwarded-For, which the gateway sets to a home address of
its country, so Akamai answers as it does a residential exit. Three things
differ on a gateway session (_gateway_for, _timeout, _checkin): the job and
?cmpId= pages of the boards on "gateway:de" never ride it (stepstone.de / .at /
.be hold the request open for a quarter of the German X-Forwarded-For
addresses; config.STEPSTONE_GATEWAY_SKIP), a try gets a short read timeout,
and the cookie jar is dropped after every call (each request leaves from
another address). Failures pause the gateway per board country
(_note_gateway_failure). Evidence in the config block.

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
import fastjson
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_client
import config
import egress
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from stepstone import sites as S
from scraper_utils import debug, memo

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
    debug.dump("STEPSTONE_DEBUG_DIR", name, text, render=json.dumps)


# ---- sessions (pooled per proxy country) ---------------------------------------------------
# A curl_cffi Session is not thread-safe, so a session is checked OUT for the
# duration of one call and returned afterwards.

class _Session:
    __slots__ = ("sess", "used", "proxy", "country", "gateway", "impersonate", "last_headers")

    def __init__(self, sess, proxy, country, impersonate, gateway=None):
        self.sess = sess
        self.proxy = proxy
        self.country = country
        self.gateway = gateway          # "gateway" / "gateway:<cc>" when `sess` is an exit_gateway.Session, else None
        self.impersonate = impersonate
        self.used = 0
        self.last_headers = PAGE_HEADERS

    def close(self):
        try:
            self.sess.close()
        except Exception:
            pass


_idle = {}                  # (country, gateway exit or None) -> [sessions]
_idle_lock = threading.Lock()
_fresh_count = 0


def _budget():
    return max(1, int(config.STEPSTONE_REQUESTS_PER_SESSION))


def _new_session(country, gateway=None):
    """A new curl session: next TLS identity in rotation, on a fresh sticky
    exit of `country`, with an empty cookie jar. `gateway` ("gateway:de") = an
    exit_gateway.Session on that gateway exit instead (egress.new_session)."""
    global _fresh_count
    identities = tuple(config.STEPSTONE_IMPERSONATIONS) or ("chrome",)
    with _idle_lock:
        impersonate = identities[_fresh_count % len(identities)]
        _fresh_count += 1
    proxy = gateway or config.stepstone_proxy(country)
    return _Session(egress.new_session(proxy, impersonate=impersonate), proxy, country, impersonate, gateway)


def _checkout(country, fresh=False, gateway=None):
    if not fresh:
        with _idle_lock:
            pool = _idle.get((country, gateway)) or []
            if pool:
                return pool.pop()
    return _new_session(country, gateway)


def _checkin(session):
    session.used += 1
    if session.used >= _budget():
        session.close()
        return
    if session.gateway:
        # Every gateway request leaves from another gateway address and announces
        # another X-Forwarded-For, so the Akamai cookies one call collected
        # (_abck, bm_sz, ak_bmsc) are not replayed by the next: a pooled
        # gateway session keeps its connection, never its jar. 2026-11-11:
        # with the jar kept 2 of 21 UK requests failed (a job page held open
        # on a session's 7th use, a 403 on its 12th); cookie-less ones did not.
        session.sess.cookies.clear()
    with _idle_lock:
        _idle.setdefault((session.country, session.gateway), []).append(session)


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


# ---- browser fallback (on the browser box) --------------------------------------------------

# Sticky residential exits per board country for the box's Chromes
# (recipes/stepstone.py navigates until a real board page is up). A refusal
# burns that country's exit: the box closes the browser, the next fallback
# call starts on a fresh one. Chrome left GKE 2026-10-22.
# STEPSTONE_BROWSER_SLOTS browsers per country are the share; the pool
# offers share x BROWSER_BURST_FACTOR candidates and the box picks an idle
# one or opens another while it has headroom (2026-11-04).
class _CountryExits(browser_client.ExitPool):
    """One sticky-exit family per board country (the pool key): the mint
    needs the country. Until 2026-11-04 get()/rotate() rebound self._mint
    per call OUTSIDE the lock, so two countries minting at once could hand
    one country the other's exit; _mint_for(key) is the hook for this."""

    def _mint_for(self, key):
        return config.stepstone_proxy(key)


_EXITS = _CountryExits("stepstone", lambda: None, size=lambda: config.STEPSTONE_BROWSER_SLOTS, burst=True)


def _box_page(country, url, label):
    try:
        _proxy, res = _EXITS.fetch_any("stepstone", country, url=url)
    except browser_client.BoxRefused as e:
        _EXITS.rotate(e.proxy, key=country)      # only the exit the box was using
        raise StepstoneBlocked(f"{label}: refused in the box browser: {e}") from e
    except browser_client.BoxUnavailable as e:
        raise StepstoneUpstreamError(f"{label}: browser box unavailable: {e}") from e
    return classify(int(res.get("status") or 200), res.get("body") or "", label, res.get("final_url") or url)


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
            data=json.dumps({"bm-verify": token_match.group(1), "pow": answer}), timeout=_timeout(session))
        resp = session.sess.get(url, headers=session.last_headers, timeout=_timeout(session), allow_redirects=True)
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
        resp = session.sess.get(url, headers=headers, timeout=_timeout(session), allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) < 10000 and _INTERSTITIAL_MARKER in resp.text:
            resp = _solve_interstitial(session, str(resp.url), resp.text, label)
    except (StepstoneBlocked, StepstoneUpstreamError):
        raise
    except Exception as e:
        raise StepstoneUpstreamError(f"request failed on {label}: {type(e).__name__}: {e}")
    return classify(resp.status_code, resp.text, label, str(resp.url))


# ---- the gateway exit in front of the residential exits ---------------------------------------
# A board country listed in config.STEPSTONE_GATEWAY_EXITS tries its gateway exit
# first (config.stepstone_gateway; evidence in the config block): a call
# makes config.STEPSTONE_GATEWAY_ATTEMPTS tries there, then the residential
# attempts exactly as before, so a gateway problem costs one failed try, not
# an outage. config.STEPSTONE_GATEWAY_FAILURES failures of one country within
# config.STEPSTONE_GATEWAY_WINDOW seconds send its calls straight to the
# residential exits for config.STEPSTONE_GATEWAY_PAUSE seconds; then the
# gateway is tried again.
_gateway_lock = threading.Lock()
_gateway_failures = {}      # board country -> time.monotonic() of its latest failures on the gateway
_gateway_paused = {}        # board country -> time.monotonic() until which its calls skip the gateway


def _gateway_for(country, url):
    """The gateway exit a call for `url` on a board of `country` tries first,
    or None: no gateway configured for the country, paused after failures,
    or a page that gateway cannot serve (config.STEPSTONE_GATEWAY_SKIP: the
    job and ?cmpId= pages of the stepstone.* boards hang for a quarter of
    the Frankfurt gateway's X-Forwarded-For addresses)."""
    gateway = config.stepstone_gateway(country)
    if not gateway or time.monotonic() < _gateway_paused.get(country, 0.0):
        return None
    if any(part in url for part in config.STEPSTONE_GATEWAY_SKIP.get(gateway, ())):
        return None
    return gateway


def _timeout(session):
    """(connect, read) of one request: a gateway try gets a short read
    timeout — pages answer there in 0.7-3.5 s, and a request the site holds
    open would otherwise cost the gateway's full 29 s."""
    return (TIMEOUT[0], config.STEPSTONE_GATEWAY_TIMEOUT) if session.gateway else TIMEOUT


def _note_gateway_failure(country, reason):
    now = time.monotonic()
    with _gateway_lock:
        recent = [t for t in _gateway_failures.get(country, ()) if now - t < config.STEPSTONE_GATEWAY_WINDOW] + [now]
        _gateway_failures[country] = recent
        if len(recent) >= int(config.STEPSTONE_GATEWAY_FAILURES):
            _gateway_failures[country] = []
            _gateway_paused[country] = now + config.STEPSTONE_GATEWAY_PAUSE
            print(f"stepstone {country}: {len(recent)} failures on the gateway exit within "
                  f"{config.STEPSTONE_GATEWAY_WINDOW} s (last: {reason}); residential exits only for "
                  f"{config.STEPSTONE_GATEWAY_PAUSE} s")


def _with_sessions(country, curl_call, browser_call, label, gateway=None):
    """Run curl_call(session) on pooled sessions of `country` (fresh ones after
    a failure) — on the gateway exit `gateway` first (_gateway_for), then on
    the country's residential exits — then browser_call() when every
    residential attempt was refused."""
    last = None
    blocked_only = True
    attempts = max(1, int(config.STEPSTONE_MAX_ATTEMPTS))
    gateway_tries = max(0, int(config.STEPSTONE_GATEWAY_ATTEMPTS)) if gateway else 0
    total = gateway_tries + attempts
    for attempt in range(1, total + 1):
        via = gateway if attempt <= gateway_tries else None
        session = None
        rate_limited = False
        try:
            # the first try of each tier takes a pooled session, a retry a fresh one
            session = _checkout(country, fresh=attempt > 1 and attempt != gateway_tries + 1, gateway=via)
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
            last, blocked_only = e, blocked_only and bool(via)
            if session is not None:
                session.close()
        except Exception as e:
            last, blocked_only = StepstoneUpstreamError(f"{label}: {type(e).__name__}: {e}"), blocked_only and bool(via)
            if session is not None:
                session.close()
        print(f"stepstone: attempt {attempt}/{total} on {label}{' through ' + via if via else ''} failed: {last}")
        if via:
            _note_gateway_failure(country, last)
        if attempt < total:
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
                          lambda: _box_page(country, url, label), label, gateway=_gateway_for(country, url))


def get_json(site, path, params=None, label=None):
    """GET a board JSON endpoint -> parsed body."""
    row = S.site(site) if isinstance(site, str) else site
    url = _url(row, path, params)
    label = label or f"{row['host']}{path}"
    headers = {**JSON_HEADERS, "accept-language": row["accept_language"], "referer": S.link(row, "/")}
    country = row["proxy_country"]
    resp = _with_sessions(country, lambda s: _curl_get(s, url, headers, label), None, label,
                          gateway=_gateway_for(country, url))
    try:
        return fastjson.loads(resp.text) if resp.text.strip() else None
    except ValueError:
        dump_debug("non_json", resp.text)
        raise StepstoneUpstreamError(f"non-JSON body on {label}")


# ---- memo + fan-out ---------------------------------------------------------------------------------

_memo = OrderedDict()
_memo_lock = threading.Lock()
MEMO_TTL = 24 * 3600
MEMO_MAX = 256


def memoized(key, fn, ttl=MEMO_TTL):
    return memo.memoized(_memo, _memo_lock, MEMO_MAX, key, fn, ttl)


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
