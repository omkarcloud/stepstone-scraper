"""The StepStone Group job boards this scraper serves, keyed by the public
`site` code. Every board runs the same stack (validated 2026-10-17): a search
page at <search_prefix>/<keyword-slug>/in-<location-slug> whose result list
is embedded as window.__PRELOADED_STATE__["app-unifiedResultlist"], a job
page whose data is window.__PRELOADED_STATE__.JobAdContent plus a JSON-LD
JobPosting, a company jobs page (same result-list state plus the company
passport) and the search bar's autosuggest JSON. What differs per board is
the host, the URL words, the language, the currency and — for the UK brands
("uk" family) — the filter parameter names, which jobs.py maps.

`site_id` is the board's id in the autosuggest API; None means it is read
from the first search page fetched (fetch.site_id).

Not served: irishjobs.ie (Akamai refuses every exit tried), jobs.ie,
caterer.com, milkround.com, cityjobs.com and the other small UK brands
(same stack, never validated — add a row here to enable one).
"""
from collections import OrderedDict
from urllib.parse import quote

SITES = OrderedDict([
    ("de", {"name": "Stepstone Germany", "host": "www.stepstone.de", "site_id": 250, "country": "DE",
            "proxy_country": "de", "language": "de", "accept_language": "de-DE,de;q=0.9,en;q=0.8",
            "search_prefix": "/jobs", "job_template": "/stellenangebote--{slug}--{id}-inline.html",
            "company_template": "/cmp/de/{slug}-{id}/jobs", "currency": "EUR", "radius_unit": "km",
            "family": "stepstone"}),
    ("at", {"name": "Stepstone Austria", "host": "www.stepstone.at", "site_id": 255, "country": "AT",
            "proxy_country": "at", "language": "de", "accept_language": "de-AT,de;q=0.9,en;q=0.8",
            "search_prefix": "/jobs", "job_template": "/stellenangebote--{slug}--{id}-inline.html",
            "company_template": "/cmp/de/{slug}-{id}/jobs", "currency": "EUR", "radius_unit": "km",
            "family": "stepstone"}),
    ("be", {"name": "Stepstone Belgium", "host": "www.stepstone.be", "site_id": 260, "country": "BE",
            "proxy_country": "be", "language": "nl", "accept_language": "nl-BE,nl;q=0.9,fr;q=0.8,en;q=0.7",
            "search_prefix": "/vacatures", "job_template": "/vacatures--{slug}--{id}-inline.html",
            "company_template": "/cmp/nl/{slug}-{id}/vacatures", "currency": "EUR", "radius_unit": "km",
            "family": "stepstone"}),
    ("nl", {"name": "Stepstone Netherlands", "host": "www.stepstone.nl", "site_id": None, "country": "NL",
            "proxy_country": "nl", "language": "nl", "accept_language": "nl-NL,nl;q=0.9,en;q=0.8",
            "search_prefix": "/vacatures", "job_template": "/vacatures--{slug}--{id}-inline.html",
            "company_template": "/cmp/nl/{slug}-{id}/vacatures", "currency": "EUR", "radius_unit": "km",
            "family": "stepstone"}),
    ("totaljobs", {"name": "Totaljobs (UK)", "host": "www.totaljobs.com", "site_id": 4, "country": "GB",
                   "proxy_country": "gb", "language": "en", "accept_language": "en-GB,en;q=0.9",
                   "search_prefix": "/jobs", "job_template": "/job/{slug}/x-job{id}",
                   "company_template": "/jobs/{slug}?cmpId={id}&cmp=1", "currency": "GBP",
                   "radius_unit": "miles", "family": "uk"}),
    ("cwjobs", {"name": "CWJobs (UK, IT)", "host": "www.cwjobs.co.uk", "site_id": None, "country": "GB",
                "proxy_country": "gb", "language": "en", "accept_language": "en-GB,en;q=0.9",
                "search_prefix": "/jobs", "job_template": "/job/{slug}/x-job{id}",
                "company_template": "/jobs/{slug}?cmpId={id}&cmp=1", "currency": "GBP",
                "radius_unit": "miles", "family": "uk"}),
    ("nijobs", {"name": "NIJobs (Northern Ireland)", "host": "www.nijobs.com", "site_id": None, "country": "GB",
                "proxy_country": "gb", "language": "en", "accept_language": "en-GB,en;q=0.9",
                "search_prefix": "/jobs", "job_template": "/job/{slug}/x-job{id}",
                "company_template": "/jobs/{slug}?cmpId={id}&cmp=1", "currency": "GBP",
                "radius_unit": "miles", "family": "uk"}),
    ("pnet", {"name": "Pnet (South Africa)", "host": "www.pnet.co.za", "site_id": 280, "country": "ZA",
              "proxy_country": "za", "language": "en", "accept_language": "en-ZA,en;q=0.9",
              "search_prefix": "/jobs", "job_template": "/jobs--{slug}--{id}-inline.html",
              "company_template": "/cmp/en/{slug}-{id}/work", "currency": "ZAR", "radius_unit": "km",
              "family": "stepstone"}),
])

DEFAULT_SITE = "de"

# host (with and without www.) -> site code
HOSTS = {}
for _code, _site in SITES.items():
    HOSTS[_site["host"]] = _code
    HOSTS[_site["host"].removeprefix("www.")] = _code

# ISO country / common aliases -> site code (the main board of that country)
COUNTRY_SITES = {"DE": "de", "AT": "at", "BE": "be", "NL": "nl", "GB": "totaljobs", "UK": "totaljobs",
                 "ZA": "pnet"}

# the words a search path may start with, per board (the English mirror of
# stepstone.de lives under /work/)
SEARCH_PREFIXES = {"/jobs", "/vacatures", "/work", "/offres-d-emploi", "/emplois"}

# The dummy slug StepStone accepts in place of the SEO words of a job URL
# (…/stellenangebote--x--13818030-inline.html and /job/x/x-job108167578 both
# resolve; the bare id without any slug is refused). Company /cmp/ pages refuse
# it, so a company known only by id goes through the search page's cmpId
# filter instead (company_path).
DUMMY_SLUG = "x"


def site(code):
    """The site row for a code, with the code itself under "code"."""
    row = SITES[code]
    return {"code": code, **row}


def link(site_row, path):
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return f"https://{site_row['host']}{path if path.startswith('/') else '/' + path}"


def slug(value):
    """'Python Developer' -> 'python-developer' the way the site builds its
    search paths (unicode letters kept: 'in-münchen'; '#' and '+' escaped:
    'c%23-entwickler')."""
    words = (value or "").strip().lower().replace("&", " ")
    words = "".join(ch if ch.isalnum() or ch in "+#.' " else " " for ch in words)
    out = "-".join(w for w in words.split() if w)
    return quote(out, safe="-.'")


def search_path(site_row, keyword=None, location=None):
    path = site_row["search_prefix"]
    if keyword:
        path += "/" + slug(keyword)
    if location:
        path += "/in-" + slug(location)
    return path


def job_path(site_row, job_id, slug_text=None):
    return site_row["job_template"].format(slug=slug_text or DUMMY_SLUG, id=job_id)


def company_path(site_row, company_id, slug_text=None):
    """The company jobs page: the board's /cmp/ URL when the slug is known
    (the /cmp/ route refuses a made-up slug), else the search page filtered to
    the company — <search_prefix>?cmpId=<id>&cmp=1 — which every board serves
    in the company context (title, passport, result list)."""
    if slug_text and slug_text != DUMMY_SLUG:
        return site_row["company_template"].format(slug=slug_text, id=company_id)
    return f"{site_row['search_prefix']}?cmpId={company_id}&cmp=1"


def public_rows():
    """The /stepstone/sites listing."""
    return [{"site": code, "name": row["name"], "link": f"https://{row['host']}", "country": row["country"],
             "language": row["language"], "currency": row["currency"], "radius_unit": row["radius_unit"],
             "filter_family": row["family"]} for code, row in SITES.items()]
