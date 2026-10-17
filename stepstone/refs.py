"""StepStone input parsing: ONE param per input that auto-detects its forms
(tripadvisor QueryOrLinkField convention, never a sibling `url`/`id` pair).
Every resolver returns plain JSON values so validated params stay usable as
a response-cache key, and raises ValueError with a user-facing message.

  site      de | at | be | nl | totaljobs | cwjobs | nijobs | pnet
            | DE / AT / GB / UK / ZA (ISO country) | www.stepstone.at
            | https://www.totaljobs.com/jobs/…                 -> "de" / "totaljobs" / …
  job       13818030 | https://www.stepstone.de/stellenangebote--…--13818030-inline.html
            | https://www.totaljobs.com/job/python-developer/acme-job108167578
            | https://www.pnet.co.za/jobs--…--4291718-inline.html
                                                              -> {"id": 13818030, "site": "de" | None}
  company   46710 | ratbacher-gmbh-46710 | https://www.stepstone.de/cmp/de/ratbacher-gmbh-46710/jobs
            | https://www.totaljobs.com/jobs/anson-mccade?cmpId=1231308&cmp=1
                                                              -> {"id": 46710, "slug": "ratbacher-gmbh" | None, "site": …}
  link      any StepStone-group search / company-jobs page URL
                                                              -> {"site": "de", "path": "/jobs/…", "query": {…}}
"""
import re
from urllib.parse import parse_qs, unquote, urlparse

from stepstone import sites as S

_ID_RE = re.compile(r"^\d{1,12}$")
_JOB_PATH_RE = re.compile(r"--(\d{3,12})-inline\.html", re.I)
_UK_JOB_RE = re.compile(r"-job(\d{3,12})(?:$|[/?#])", re.I)
_COMPANY_PATH_RE = re.compile(r"/cmp/[a-z]{2}/([^/?#]*?)-?(\d{1,12})(?:/|$)", re.I)
_SLUG_ID_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-&' ]*?)[-_ ](\d{1,12})$")
_SLUG_BAD = re.compile(r"[/?#\\<>\"]")

SITE_HINT = ("Must be a StepStone board: " + ", ".join(S.SITES) +
             " (or its ISO country / host / a link on it).")
JOB_HINT = ("Must be a StepStone job id (e.g. 13818030) or a job link on one of the boards "
            "(…/stellenangebote--…--13818030-inline.html, …/job/…-job108167578).")
COMPANY_HINT = ("Must be a StepStone company id (46710), 'slug-id' as in the board's /cmp/ links "
                "(ratbacher-gmbh-46710), or a company link (…/cmp/de/ratbacher-gmbh-46710/jobs, "
                "…/jobs/acme?cmpId=1231308).")
LINK_HINT = "Must be a search or company-jobs page link on one of the StepStone boards: " + ", ".join(S.SITES) + "."


def _parse_url(value):
    """-> (site code, path, query dict) when value is a URL / host path on a
    known board; None when it is not URL-like; ValueError for another site."""
    raw = (value or "").strip()
    low = raw.lower()
    if low.startswith("//"):
        raw = "https:" + raw
    elif not low.startswith(("http://", "https://")):
        head = low.split("/", 1)[0]
        if head not in S.HOSTS:
            return None
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    code = S.HOSTS.get(host)
    if not code:
        raise ValueError(f"'{host}' is not one of the StepStone boards served ({', '.join(S.SITES)}).")
    return code, unquote(parsed.path), parse_qs(parsed.query)


# ---- site ----------------------------------------------------------------------------------

def resolve_site(value):
    raw = (value or "").strip()
    if not raw:
        return S.DEFAULT_SITE
    low = raw.lower()
    if low in S.SITES:
        return low
    if raw.upper() in S.COUNTRY_SITES:
        return S.COUNTRY_SITES[raw.upper()]
    try:
        url = _parse_url(raw) if ("." in raw or "/" in raw) else None
    except ValueError:
        raise ValueError(SITE_HINT)
    if url:
        return url[0]
    raise ValueError(f"'{raw}' is not a StepStone board. " + SITE_HINT)


# ---- jobs -----------------------------------------------------------------------------------

def resolve_job(value):
    raw = (value or "").strip()
    if _ID_RE.match(raw):
        return {"id": int(raw), "site": None}
    try:
        url = _parse_url(raw)
    except ValueError as e:
        raise ValueError(f"{e} {JOB_HINT}")
    if not url:
        raise ValueError(JOB_HINT)
    code, path, query = url
    match = _JOB_PATH_RE.search(path) or _UK_JOB_RE.search(path)
    if match:
        return {"id": int(match.group(1)), "site": code}
    for name in ("offerId", "listingId", "id"):
        for candidate in query.get(name, []):
            if _ID_RE.match(candidate.strip()):
                return {"id": int(candidate.strip()), "site": code}
    raise ValueError(JOB_HINT)


def resolve_jobs(value, max_items=10):
    out = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            job = resolve_job(item)
        except ValueError as e:
            raise ValueError(f"'{item}': {e}")
        if job not in out:
            out.append(job)
    if not out:
        raise ValueError("Must list at least one job id or link (comma-separated).")
    if len(out) > max_items:
        raise ValueError(f"At most {max_items} jobs.")
    return out


# ---- companies -------------------------------------------------------------------------------

def _slug(value):
    slug = "-".join((value or "").strip().lower().split())
    if not slug or _SLUG_BAD.search(slug) or len(slug) > 150:
        return None
    return slug


def resolve_company(value):
    raw = (value or "").strip()
    if not raw:
        raise ValueError(COMPANY_HINT)
    if _ID_RE.match(raw):
        return {"id": int(raw), "slug": None, "site": None}
    try:
        url = _parse_url(raw)
    except ValueError as e:
        raise ValueError(f"{e} {COMPANY_HINT}")
    if url:
        code, path, query = url
        match = _COMPANY_PATH_RE.search(path)
        if match:
            return {"id": int(match.group(2)), "slug": _slug(match.group(1)), "site": code}
        for candidate in query.get("cmpId", []):
            if _ID_RE.match(candidate.strip()):
                segments = [s for s in path.split("/") if s]
                slug = _slug(segments[1]) if len(segments) >= 2 and segments[0] in ("jobs", "vacatures", "work") else None
                return {"id": int(candidate.strip()), "slug": slug, "site": code}
        raise ValueError("StepStone company links must be company pages (/cmp/<lang>/<slug>-<id>/… or …?cmpId=<id>). " + COMPANY_HINT)
    match = _SLUG_ID_RE.match(raw)
    if match:
        return {"id": int(match.group(2)), "slug": _slug(match.group(1)), "site": None}
    raise ValueError(f"'{raw}' has no company id. StepStone offers no company-name search; " + COMPANY_HINT)


# ---- listing pages ------------------------------------------------------------------------------

def resolve_listing(value):
    raw = (value or "").strip()
    try:
        url = _parse_url(raw)
    except ValueError as e:
        raise ValueError(f"{e} {LINK_HINT}")
    if not url:
        raise ValueError(LINK_HINT)
    code, path, query = url
    path = path.rstrip("/") or "/"
    first = "/" + path.split("/")[1] if path != "/" else "/"
    if first not in S.SEARCH_PREFIXES and not path.startswith("/cmp/"):
        raise ValueError("The link must be a job search (/jobs/…, /vacatures/…) or company page (/cmp/…). " + LINK_HINT)
    if _JOB_PATH_RE.search(path) or _UK_JOB_RE.search(path):
        raise ValueError("That is a single job's link — use /stepstone/jobs/details for it.")
    return {"site": code, "path": path, "query": {k: v for k, v in sorted(query.items()) if k not in ("page", "rltr", "siteId", "action")}}
