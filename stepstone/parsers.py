"""Raw StepStone pages -> clean API objects. Pure functions, no network;
every parser tolerates missing / null / wrong-typed fields and never raises
on a partial payload.

Inputs are the JSON blobs the boards embed in their HTML (extract_app):

  app-unifiedResultlist       search / company-jobs pages: items, filters with
                              counts, pagination, the parsed query
                              (categorization), SEO extras
  JobAdContent                job pages: header (contract, work type, salary),
                              text sections, company card with rating, company
                              passport, apply data, multimedia
  RecommenderWidget_listing_list   "popular with other seekers" jobs on a job page
  SeoRelatedLinks             related search links on a job page
  SeoCompanyPassportData      company passport on company pages
plus the JSON-LD JobPosting (json_ld) and the autosuggest JSON.

Intentionally dropped everywhere: tracking (rltr query params, userHashId,
optimizely / amplitude / tealium blobs, x-stst headers, lvp click-prediction
scores, metaData.positionOnPage), UI state and labels (theme tokens, label
dictionaries, abTests, loginWall / applyFormTypeVerification flags, filter
open/expanded state, sorting and pagination link templates), duplicated
dates (periodPostedDate / publishFromDate == datePosted), per-size logo
variants, fingerprintCount / jobPostingSequence / cvToJobScore /
travelTime (always null for anonymous visitors), the Google Maps API key and
the commute-time service config.
"""
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from stepstone.shared import as_dict, as_list, g, html_to_text, iso_datetime, multiline, rounded, text, to_int
from stepstone import sites as S

_UNDEFINED_RE = re.compile(r":\s*undefined\b")
_TAG_RE = re.compile(r"<[^>]+>")
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_TRACKING_PARAMS = {"rltr", "cmp", "intcid", "utm_source", "utm_medium", "utm_campaign"}

# result-list filter type -> public name (the search param that takes its option ids)
FILTER_NAMES = {
    "age": "posted_within", "unifiedSalary": "salary_min", "workFromHome": "remote",
    "worktypes": "work_type", "jobTypes": "job_type", "companyTypes": "company_type",
    "disciplines": "discipline", "contractTypes": "contract_type", "experiences": "experience",
    "skills": "skill", "categories": "category", "sectors": "sector", "cities": "city",
    "regions": "region", "applicationMethod": "application_method", "detectedLanguages": "language",
}
# option ids of the filters whose public values are words, not the site's ids
AGE_VALUES = {"age_1": "1_day", "age_7": "7_days", "1": "1_day", "3": "3_days", "7": "7_days", "14": "14_days"}
REMOTE_VALUES = {"1": "full", "2": "partial", "0": "none"}
WORK_TYPE_VALUES = {"80001": "full_time", "80002": "part_time"}
APPLICATION_VALUES = {"INTERNAL": "quick_apply", "EXTERNAL": "company_website"}
SECTION_NAMES = {"main": "direct", "semantic": "related", "regional": "nearby", "family": "related_company",
                 "recommended": "recommended"}
PERIODS = {"annually": "year", "yearly": "year", "jahr": "year", "jaar": "year", "annum": "year", "year": "year",
           "monthly": "month", "monat": "month", "maand": "month", "month": "month",
           "weekly": "week", "woche": "week", "week": "week",
           "daily": "day", "tag": "day", "dag": "day", "day": "day",
           "hourly": "hour", "stunde": "hour", "uur": "hour", "hour": "hour"}
CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD", "R": "ZAR", "CHF": "CHF"}
SUGGESTION_TYPES = {"TYPE_JD": "job_title", "TYPE_COMPANY": "company", "TYPE_CITY": "location",
                    "TYPE_LOCATION": "location", "TYPE_BRAND": "company"}


# ---- embedded state --------------------------------------------------------------------------

def _balanced(source, start):
    """The {...} object starting at the first '{' at/after `start`, with
    string-aware brace matching; None when unbalanced."""
    i = source.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(source)):
        ch = source[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[i:j + 1]
    return None


def _load(raw):
    if not raw:
        return None
    try:
        return json.loads(_UNDEFINED_RE.sub(":null", raw))
    except ValueError:
        return None


def extract_app(html, name):
    """window.__PRELOADED_STATE__["<name>"] = {...} or
    window.__PRELOADED_STATE__.<name> = {...} -> the object. A JS literal of
    the form { props: {...}, ... } (JobAdContent and the other viewlets) yields
    its props object."""
    html = html or ""
    for key in (f'window.__PRELOADED_STATE__["{name}"] = ', f"window.__PRELOADED_STATE__.{name} = "):
        i = html.find(key)
        if i < 0:
            continue
        start = i + len(key)
        brace = html.find("{", start)
        if brace < 0:
            return None
        props = html.find("props:", brace, brace + 200)
        if 0 <= props and html[brace + 1:props].strip() == "":
            return _load(_balanced(html, props + 6))
        return _load(_balanced(html, start))
    return None


def json_ld(html, type_name):
    """The first JSON-LD block of `type_name` ("JobPosting") in the page."""
    for block in _LD_RE.findall(html or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == type_name:
                return item
    return None


# ---- small helpers ------------------------------------------------------------------------------

def plain(value):
    """HTML-ish snippet (<strong>Python</strong>) -> collapsed plain text."""
    value = text(_TAG_RE.sub(" ", value)) if isinstance(value, str) else None
    return value or None


def clean_link(url, site_row=None):
    """Absolute link without StepStone's tracking params (rltr, cmp, intcid)."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip().replace("\\u002F", "/").replace("\\u0026", "&")
    if site_row is not None and not url.startswith(("http://", "https://")):
        url = S.link(site_row, url)
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _float(value):
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _period(value):
    value = text(value)
    if not value:
        return None
    low = value.lower().replace("per ", "").replace("pro ", "").strip()
    return PERIODS.get(low, low)


def _currency(code, symbol, site_row=None):
    code = text(code)
    if code and len(code) == 3 and code.isalpha():
        return code.upper()
    symbol = text(symbol)
    if symbol in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[symbol]
    return site_row["currency"] if site_row else None


_MONEY_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)")


def _money_values(label):
    """Every money figure in a label ('£50,000 - £60,000 per annum' -> [50000, 60000])."""
    out = []
    for whole, cents in _MONEY_RE.findall(label or ""):
        digits = re.sub(r"[.,]", "", whole)
        try:
            value = float(digits + ("." + cents if cents else ""))
        except ValueError:
            continue
        if value >= 100 or cents:          # skip "30 hours", "5 days"
            out.append(int(value) if value.is_integer() else round(value, 2))
    return out


def salary_from_text(label, site_row=None):
    """'Up to £80,000 per annum' / '45.000 - 55.000 € pro Jahr' -> a salary object."""
    label = text(label)
    if not label:
        return None
    values = _money_values(label)
    low = label.lower()
    period = None
    for word, canonical in PERIODS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            period = canonical
            break
    symbol = next((s for s in ("€", "£", "$", "CHF") if s in label), None)
    if symbol is None and site_row and site_row["currency"] == "ZAR" and re.search(r"\bR\s?\d", label):
        symbol = "R"
    salary = {"text": label, "min": None, "max": None, "exact": None,
              "currency": _currency(None, symbol, site_row) if (symbol or values) else None,
              "period": period, "is_predicted": False}
    if len(values) >= 2:
        salary["min"], salary["max"] = min(values[:2]), max(values[:2])
    elif len(values) == 1:
        if re.search(r"\b(up to|bis zu|bis|tot|max)\b", low):
            salary["max"] = values[0]
        elif re.search(r"\b(from|ab|vanaf|minimum|min\.?|starting|\+)", low):
            salary["min"] = values[0]
        else:
            salary["exact"] = values[0]
    return salary


def salary_from_unified(unified, site_row=None, fallback_text=None):
    """The result list's unifiedSalary (min/max/exactValue, Predicted or
    stated) -> a salary object; None when the listing shows none."""
    unified = as_dict(unified)
    stated = fallback_text if isinstance(fallback_text, str) and fallback_text.strip() else None
    if not unified or not (unified.get("salaryAvailable") or unified.get("min") or unified.get("max")
                           or unified.get("exactValue")):
        return salary_from_text(stated, site_row) if stated else None
    salary_type = text(unified.get("salaryType")) or ""
    return {
        "text": stated or text(unified.get("description")),
        "min": to_int(unified.get("min")),
        "max": to_int(unified.get("max")),
        "exact": to_int(unified.get("exactValue")),
        "currency": _currency(unified.get("currencyIso"), unified.get("currency"), site_row),
        "period": _period(unified.get("period")),
        "is_predicted": salary_type.lower() == "predicted",
    }


def salary_from_ce(ce, site_row=None, wage_type=None):
    """The job page's CESalary (StepStone's salary estimate) -> a salary object."""
    ce = as_dict(ce)
    if not ce or not (ce.get("min") or ce.get("max")):
        return None
    out = {
        "text": None,
        "min": to_int(ce.get("min")),
        "max": to_int(ce.get("max")),
        "exact": None,
        "currency": _currency(ce.get("currencyIso"), ce.get("currency"), site_row),
        "period": _period(ce.get("period")),
        "is_predicted": bool(ce.get("isPredicted", True)),
    }
    if wage_type:
        out["wage_type"] = text(wage_type)          # "brutto" (gross) on German boards
    return out


def _remote(value):
    """workFromHome "0|1|2" (result list) or a label -> none | full | partial."""
    if value is None or value == "":
        return None
    key = str(value).strip()
    if key in REMOTE_VALUES:
        return REMOTE_VALUES[key]
    low = key.lower()
    if "nur" in low or "fully" in low or low in ("remote", "work from home", "home-office"):
        return "full"
    if "teilweise" in low or "partial" in low or "hybrid" in low or "möglich" in low:
        return "partial"
    return None


class _ListItems(HTMLParser):
    """<ul><li>…</li></ul> -> the items' texts (benefit lists)."""

    def __init__(self):
        super().__init__()
        self.items = []
        self._depth = 0
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self._depth += 1
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "li" and self._depth:
            self._depth -= 1
            item = text("".join(self._buf))
            if item:
                self.items.append(item)
            self._buf = []

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def list_items(html):
    """An HTML list -> its items as plain strings (empty list when none)."""
    if not isinstance(html, str) or "<li" not in html:
        plain_text = html_to_text(html) if isinstance(html, str) else None
        return [plain_text] if plain_text else []
    parser = _ListItems()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return [t for t in (html_to_text(html) or "").split("\n- ") if t]
    return parser.items


def _labels(item):
    labels = []
    for row in as_list(item.get("labels")) + as_list(item.get("topLabels")):
        row = as_dict(row)
        kind = text(row.get("type"))
        label = text(row.get("label"))
        if kind or label:
            labels.append({"type": kind, "label": label})
    return labels


def _site_of(url, default_row):
    host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    code = S.HOSTS.get(host)
    return S.site(code) if code else default_row


# ---- result list ---------------------------------------------------------------------------------

def job_item(item, site_row):
    """One result-list card -> a job summary."""
    item = as_dict(item)
    link = clean_link(item.get("url"), site_row)
    labels = _labels(item)
    kinds = {l["type"] for l in labels}
    partnership = as_dict(item.get("partnership"))
    company_link = clean_link(item.get("companyUrl"), site_row)
    return {
        "id": to_int(item.get("id")),
        "global_id": text(item.get("harmonisedId")),
        "title": text(item.get("title")),
        "link": link,
        "site": _site_of(link, site_row)["code"],
        "company": {
            "id": to_int(item.get("companyId")),
            "name": text(item.get("companyName")),
            "link": company_link,
            "logo_link": clean_link(item.get("companyLogoUrl"), site_row),
            "is_anonymous": bool(item.get("isAnonymous")),
        },
        "location": {"text": text(item.get("location")), "postcode": text(item.get("postCode"))},
        "snippet": plain(item.get("textSnippet")),
        "benefits": list_items(item.get("jobBenefitsHtml")),
        "salary": salary_from_unified(item.get("unifiedSalary"), site_row, item.get("salary")),
        "remote": _remote(item.get("workFromHome")),
        "skills": [s for s in (text(x) if not isinstance(x, dict) else text(x.get("label") or x.get("name"))
                               for x in as_list(item.get("skills"))) if s],
        "labels": [l["label"] or l["type"] for l in labels],
        "is_quick_apply": "QUICK_APPLY" in kinds,
        "requires_cover_letter": False if "NO_COVER_LETTER" in kinds else None,
        "is_sponsored": bool(item.get("isSponsored")),
        "is_highlighted": bool(item.get("isHighlighted")),
        "is_top_job": bool(item.get("isTopJob")),
        "match_type": SECTION_NAMES.get(text(item.get("section")) or "", text(item.get("section"))),
        "posted_at": iso_datetime(item.get("datePosted")),
        "expires_at": iso_datetime(item.get("publishToDate")),
        "source_site": text(partnership.get("sourceSiteFriendlyName")) if partnership.get("isPartnershipJob") else None,
        "is_cross_posted": bool(partnership.get("isCrossPosted")) or None,
        "position": to_int(g(item, "metaData", "positionAbsolute")),
    }


def _option_value(filter_type, option_id):
    if filter_type == "age":
        return AGE_VALUES.get(option_id, option_id)
    if filter_type == "workFromHome":
        return REMOTE_VALUES.get(option_id, option_id)
    if filter_type == "worktypes":
        return WORK_TYPE_VALUES.get(option_id, option_id)
    if filter_type == "applicationMethod":
        return APPLICATION_VALUES.get(option_id, option_id)
    if filter_type == "unifiedSalary":
        return to_int(option_id)
    return option_id


def filters(state, site_row):
    """The result list's facets -> [{name, label, param, options:[{value, label, count, is_selected}]}].
    `value` is what the matching search param takes."""
    labels = as_dict(g(state, "labels", "filters", "categories"))
    out = []
    for block in as_list(g(state, "searchResults", "filters")):
        block = as_dict(block)
        kind = text(block.get("type"))
        name = FILTER_NAMES.get(kind)
        if not name:
            continue
        options = []
        for child in as_list(block.get("children")):
            child = as_dict(child)
            option_id = text(child.get("id"))
            label = text(child.get("text"))
            if option_id is None or (label is None and kind != "unifiedSalary"):
                continue
            options.append({"value": _option_value(kind, option_id), "label": label,
                            "count": to_int(child.get("count")), "is_selected": bool(child.get("selected"))})
        if not options:
            continue
        out.append({"name": name, "label": text(labels.get(kind)), "param": name,
                    "is_active": bool(block.get("active")), "options": options})
    return out


def _applied(categorization):
    """categorization -> {public filter name: value} for the filters in use."""
    cat = as_dict(categorization)
    names = {"age": "posted_within", "unifiedSalary": "salary_min", "workFromHome": "remote",
             "worktypes": "work_type", "disciplines": "discipline", "contractTypes": "contract_type",
             "experiences": "experience", "skills": "skill", "categories": "category", "sectors": "sector",
             "cities": "city", "regions": "region", "applicationMethod": "application_method",
             "detectedLanguages": "language", "companies": "company", "companyId": "company"}
    out = {}
    for key, name in names.items():
        value = cat.get(key)
        if value in (None, "", [], {}):
            continue
        raw = str(value)
        if key == "age":
            out[name] = AGE_VALUES.get(raw, raw)
        elif key == "workFromHome":
            out[name] = REMOTE_VALUES.get(raw, raw)
        elif key == "worktypes":
            out[name] = WORK_TYPE_VALUES.get(raw, raw)
        elif key == "applicationMethod":
            out[name] = APPLICATION_VALUES.get(raw, raw)
        elif key == "unifiedSalary":
            out[name] = to_int(raw)
        elif "," in raw:
            out[name] = [v for v in raw.split(",") if v]
        else:
            out[name] = raw
    return out


def _related_searches(results, site_row):
    out = []
    seen = set()
    for block in as_list(results.get("filters")):
        if as_dict(block).get("type") != "relatedJobs":
            continue
        for child in as_list(as_dict(block).get("children")):
            child = as_dict(child)
            title = text(child.get("text"))
            link = clean_link(child.get("link"), site_row) if child.get("link") else None
            if title and title not in seen:
                seen.add(title)
                out.append({"title": title, "link": link})
    return out


def search_results(state, site_row):
    """app-unifiedResultlist -> {search, jobs, counts, related_searches,
    related_skills, pagination}."""
    results = as_dict(g(state, "searchResults"))
    meta = as_dict(results.get("meta"))
    cat = as_dict(results.get("categorization"))
    pagination = as_dict(results.get("unifiedPagination"))
    seo = as_dict(meta.get("seo"))
    counters = as_dict(meta.get("counters"))
    feedbacks = as_dict(meta.get("feedbacks"))
    info = as_dict(results.get("resultFilterInfo"))
    jobs = [job_item(item, site_row) for item in as_list(results.get("items")) if as_dict(item).get("id")]
    per_page = to_int(cat.get("limit")) or 25
    total = to_int(meta.get("total"))
    page = to_int(pagination.get("page")) or 1
    total_pages = to_int(pagination.get("pageCount"))
    if total_pages is None and total is not None:
        total_pages = -(-total // per_page)
    spellcheck = None
    if info.get("spellcheckRedirected") or info.get("spellcheckBestSuggestion"):
        spellcheck = {"was_redirected": bool(info.get("spellcheckRedirected")),
                      "suggestion": text(info.get("spellcheckBestSuggestion")),
                      "link": clean_link(info.get("spellcheckBestSuggestionUrl"), site_row) or None}
    return {
        "search": {
            "site": site_row["code"],
            "keyword": text(cat.get("what")) or text(info.get("what")),
            "location": text(cat.get("where")) or text(info.get("where")),
            "radius": to_int(cat.get("radius")),
            "radius_unit": site_row["radius_unit"],
            "sort": text(cat.get("orderBy")),
            "filters": _applied(cat),
            "link": clean_link(meta.get("normalizedUrl") or meta.get("baseUrl"), site_row),
            "canonical_link": clean_link(seo.get("canonicalUrl"), site_row),
            "discipline": text(seo.get("discipline")),
            "query_type": text(feedbacks.get("queryType")),
            "spellcheck": spellcheck,
        },
        "jobs": jobs,
        "counts": {
            "total": total,
            "direct": to_int(counters.get("main")),
            "related": to_int(counters.get("semantic")),
            "nearby": to_int(counters.get("regional")),
            "related_company": to_int(counters.get("family")),
            "recommended": to_int(counters.get("recommended")),
            "part_time": to_int(seo.get("partTimeJobsCount")),
            "temporary": to_int(seo.get("temporaryJobsCount")),
            "quick_apply": to_int(g(meta, "featureCounters", "offersWithQuickApplyLabel")),
            "no_cover_letter": to_int(g(meta, "featureCounters", "offersWithNoCoverLetterLabel")),
        },
        "related_searches": _related_searches(results, site_row),
        "related_skills": [{"name": text(s.get("label")), "link": clean_link(s.get("url"), site_row)}
                           for s in (as_dict(x) for x in as_list(seo.get("skills"))) if text(s.get("label"))],
        "breadcrumbs": [{"label": text(b.get("label")), "link": clean_link(b.get("url"), site_row) if b.get("url") else None}
                        for b in (as_dict(x) for x in as_list(results.get("breadcrumbs"))) if text(b.get("label"))],
        "pagination": {"page": page, "items_per_page": per_page, "total_count": total, "total_pages": total_pages},
    }


# ---- job page ------------------------------------------------------------------------------------

def _sections(props):
    out = []
    for section in as_list(props.get("textSections")):
        section = as_dict(section)
        html = section.get("content") if isinstance(section.get("content"), str) else None
        body = html_to_text(html)
        if not body and not text(section.get("title")):
            continue
        out.append({"name": text(section.get("name")), "title": text(section.get("title")) or None,
                    "text": body, "html": html.strip() if html else None})
    return out


def _location(props, ld, redux):
    address = as_dict(g(ld, "jobLocation", "address"))
    geo = as_dict(g(ld, "jobLocation", "geo"))
    loc = as_dict(redux.get("location"))
    meta = as_dict(g(props, "listingHeader", "listingData", "metaData"))
    country = text(address.get("addressCountry"))
    location_text = text(meta.get("location")) or text(props.get("jobAdLocation")) or text(loc.get("addressText"))
    street = text(address.get("streetAddress"))
    return {
        "text": location_text,
        # German boards put the multi-city list into streetAddress: that is no street
        "street": street if street and street != location_text else None,
        "city": text(address.get("addressLocality")),
        "region": text(address.get("addressRegion")),
        "postcode": text(address.get("postalCode")),
        "country": country.upper() if country else None,
        "latitude": _float(loc.get("latitude")) if loc.get("latitude") else _float(geo.get("latitude")),
        "longitude": _float(loc.get("longitude")) if loc.get("longitude") else _float(geo.get("longitude")),
    }


def company_passport(passport, site_row):
    """companyPassportData (job and company pages) -> the company profile fields."""
    passport = as_dict(passport)
    if not passport:
        return {}
    about = []
    for section in as_list(passport.get("customSections")):
        section = as_dict(section)
        html = section.get("content") if isinstance(section.get("content"), str) else None
        if text(section.get("title")) or html_to_text(html):
            about.append({"title": text(section.get("title")), "text": html_to_text(html), "html": html})
    return {
        "id": to_int(passport.get("id")),
        "name": text(passport.get("name")),
        "link": clean_link(passport.get("resultListUrl"), site_row),
        "logo_link": clean_link(passport.get("logoUrl"), site_row),
        "hero_image_link": clean_link(passport.get("heroImageUrl"), site_row),
        "address": text(passport.get("address")),
        "employees": text(passport.get("employees")),
        "founded": to_int(passport.get("founded")),
        "industries": [t for t in (text(x) if not isinstance(x, dict) else text(x.get("name")) for x in as_list(passport.get("industries"))) if t],
        "jobs_count": to_int(passport.get("jobsCount")),
        "about": about,
        "benefits": [t for t in (text(x) if not isinstance(x, dict) else (text(x.get("label")) or text(x.get("name")))
                                 for x in as_list(passport.get("benefits"))) if t],
        "images": [clean_link(as_dict(x).get("url"), site_row) for x in as_list(passport.get("images")) if as_dict(x).get("url")],
        "videos": [clean_link(as_dict(x).get("url"), site_row) if isinstance(x, dict) else clean_link(x, site_row)
                   for x in as_list(passport.get("videos")) if x],
        "is_parent_company": bool(passport.get("isParentCompany")),
        "is_anonymous": bool(passport.get("isAnonymous")),
        "has_passport": bool(passport.get("isCompanyPassportEnabled")),
    }


def _job_company(props, redux, site_row):
    data = as_dict(g(props, "listingHeader", "companyData"))
    card = as_dict(props.get("companyCard")) or as_dict(redux.get("companyCard"))
    rating = as_dict(card.get("rating"))
    out = {
        "id": to_int(data.get("id")) or to_int(card.get("companyLegacyId")) or to_int(props.get("legacyCompanyId")),
        "name": text(data.get("name")) or text(card.get("name")),
        "link": clean_link(data.get("companyNameResultPageUrl") or g(card, "urls", "resultList"), site_row),
        "logo_link": clean_link(data.get("logoUrl") or g(card, "urls", "logo"), site_row),
        "is_anonymous": bool(data.get("isAnonymous")),
        "rating": {"average": rounded(rating.get("overallRatingAvg"), 2), "count": to_int(rating.get("surveysCount"))}
                  if rating.get("surveysCount") else None,
        "employees": text(card.get("employees")),
        "sectors": [t for t in (text(as_dict(s).get("name")) for s in as_list(card.get("sectors"))) if t],
        "story_link": clean_link(g(card, "companyStory", "url"), site_row) or None,
    }
    passport = company_passport(props.get("companyPassportData"), site_row)
    for key in ("hero_image_link", "address", "founded", "industries", "about", "benefits", "images", "videos",
                "is_parent_company"):
        out[key] = passport.get(key) if passport else ([] if key in ("industries", "about", "benefits", "images", "videos") else None)
    out["has_passport"] = bool(passport.get("has_passport")) if passport else False
    out["jobs_count"] = (passport or {}).get("jobs_count") or to_int(card.get("jobsCount")) or None
    if passport and not out["link"]:
        out["link"] = passport.get("link")
    if passport and not out["logo_link"]:
        out["logo_link"] = passport.get("logo_link")
    return out


def similar_jobs(widget, site_row):
    """RecommenderWidget_listing_list -> job summaries."""
    out = []
    for item in as_list(g(widget, "jobAdsData", "items")):
        item = as_dict(item)
        if not item.get("id"):
            continue
        link = clean_link(item.get("url"), site_row)
        out.append({
            "id": to_int(item.get("id")),
            "global_id": text(item.get("harmonisedId")),
            "title": text(item.get("title")),
            "normalized_title": text(item.get("normalizedTitle")),
            "link": link,
            "company": {"id": to_int(item.get("companyId")), "name": text(item.get("companyName")),
                        "link": clean_link(item.get("companyUrl"), site_row),
                        "logo_link": clean_link(item.get("companyLogoUrl"), site_row)},
            "location": {"text": text(item.get("location"))},
            "salary": salary_from_text(item.get("salary"), site_row),
            "remote": _remote(item.get("workFromHome")) or ("partial" if item.get("isWorkFromHomePossible") else None),
            "labels": [text(l.get("label") if isinstance(l, dict) else l) for l in as_list(item.get("labels")) if l],
            "posted_at": iso_datetime(item.get("datePosted")),
        })
    return out


def related_links(widget, site_row):
    """SeoRelatedLinks -> [{title, link, type}]."""
    out = []
    for row in as_list(g(widget, "relatedLinks")):
        row = as_dict(row)
        title = text(row.get("name"))
        if not title:
            continue
        kind = text(row.get("linkType")) or ""
        out.append({"title": title, "link": clean_link(row.get("url"), site_row),
                    "type": {"Jd": "job_title", "Discipline": "discipline", "Subdiscipline": "subdiscipline"}.get(kind, "related")})
    return out


def job_details(html, site_row, job_id=None):
    """A job page -> the full job object, or None when the page carries no job
    (redirected to a result list, offline shell without data)."""
    props = extract_app(html, "JobAdContent")
    if not props:
        return None
    ld = json_ld(html, "JobPosting") or {}
    header = as_dict(props.get("listingHeader"))
    listing = as_dict(header.get("listingData"))
    meta = as_dict(listing.get("metaData"))
    redux = as_dict(props.get("reduxPreloadedState"))
    job_ad = as_dict(redux.get("jobAd"))
    apply_data = as_dict(g(redux, "applyNow", "applyData"))
    multimedia = as_dict(redux.get("multimedia"))
    predicted = as_dict(g(redux, "predictedSalary", "salary"))
    sections = _sections(props)
    identifier = to_int(listing.get("id")) or to_int(props.get("jobAdId")) or to_int(props.get("legacyJobId")) or job_id
    link = clean_link(ld.get("url"), site_row) or S.link(site_row, S.job_path(site_row, identifier))
    salary = (salary_from_ce(header.get("CESalary") or job_ad.get("CESalary"), site_row, predicted.get("wageType"))
              or salary_from_text(g(header, "UKSalary", "description") or g(job_ad, "UKSalary", "description"), site_row))
    online_status = text(props.get("onlineStatus"))
    work_type = text(meta.get("workType"))
    contract = text(meta.get("contractType"))
    description_html = "\n".join(s["html"] for s in sections if s.get("html")) or None
    return {
        "id": identifier,
        "global_id": text(listing.get("externalId")) or text(props.get("harmonisedJobId")),
        "title": text(listing.get("title")) or text(props.get("jobAdTitle")) or text(ld.get("title")),
        "link": link,
        "site": site_row["code"],
        "language": text(props.get("offerLanguage")) or text(props.get("lang")),
        "is_online": (online_status.lower() == "online") if online_status else (not job_ad.get("isOffline")),
        "is_expired": bool(job_ad.get("isIgnoredExpired") or header.get("isIgnoredExpired")),
        "posted_at": iso_datetime(meta.get("onlineDate") or ld.get("datePosted")),
        "expires_at": iso_datetime(ld.get("validThrough")),
        "employment_type": text(ld.get("employmentType")),
        "contract_type": contract,
        "work_types": [t for t in (text(p) for p in re.split(r"[;,]", work_type or "")) if t],
        "remote": _remote(work_type) or _remote(contract),
        "industry": text(ld.get("industry")),
        "location": _location(props, ld, redux),
        "salary": salary,
        "badges": [b for b in (text(x) for x in as_list(header.get("badges") or job_ad.get("badges"))) if b],
        "is_direct_apply": ld.get("directApply") if isinstance(ld.get("directApply"), bool) else None,
        "is_apply_blocked": bool(apply_data.get("isApplyBlocked")) or None,
        "is_free_listing": bool(job_ad.get("isFreeListing")) or None,
        "description": {"text": "\n\n".join(s["text"] for s in sections if s.get("text")) or html_to_text(ld.get("description")),
                        "html": description_html or (ld.get("description") if isinstance(ld.get("description"), str) else None)},
        "sections": sections,
        "hero_image_link": clean_link(g(props, "heroImage", "heroImages", 0, "desktop"), site_row)
                           if g(props, "heroImage", "shouldBeDisplayed") else None,
        "images": [clean_link(as_dict(x).get("url") if isinstance(x, dict) else x, site_row) for x in as_list(multimedia.get("images")) if x],
        "videos": [clean_link(as_dict(x).get("url") if isinstance(x, dict) else x, site_row) for x in as_list(multimedia.get("videos")) if x],
        "company": _job_company(props, redux, site_row),
        "similar_jobs": similar_jobs(extract_app(html, "RecommenderWidget_listing_list"), site_row),
        "related_searches": related_links(extract_app(html, "SeoRelatedLinks"), site_row),
    }


# ---- company page --------------------------------------------------------------------------------

def company_details(html, site_row, company_id=None):
    """A company jobs page -> the company profile (passport when the board
    has one, else what the result list knows), or None when the page is not
    a company page."""
    state = extract_app(html, "app-unifiedResultlist")
    passport_app = extract_app(html, "SeoCompanyPassportData")
    if not state and not passport_app:
        return None
    results = as_dict(g(state, "searchResults"))
    meta = as_dict(results.get("meta"))
    items = [as_dict(i) for i in as_list(results.get("items"))]
    first = next((i for i in items if i.get("companyId")), items[0] if items else {})
    passport = company_passport(g(passport_app, "companyPassportData"), site_row)
    context = text(g(state, "appMetadata", "context")) or ""
    if not passport and context.lower() != "company" and not first.get("companyId"):
        return None
    link = clean_link(meta.get("normalizedUrl") or meta.get("baseUrl"), site_row) or passport.get("link")
    id_in_link = re.search(r"[?&]cmpId=(\d+)|/cmp/[a-z]{2}/[^/?#]*?-?(\d+)(?:/|$)", link or "")
    out = {
        "id": passport.get("id") or to_int(first.get("companyId")) or company_id
              or (to_int(id_in_link.group(1) or id_in_link.group(2)) if id_in_link else None),
        "name": passport.get("name") or text(first.get("companyName")) or text(g(results, "categorization", "what")),
        "link": link,
        "site": site_row["code"],
        "logo_link": passport.get("logo_link") or clean_link(first.get("companyLogoUrl"), site_row),
        "hero_image_link": passport.get("hero_image_link"),
        "address": passport.get("address"),
        "employees": passport.get("employees"),
        "founded": passport.get("founded"),
        "industries": passport.get("industries") or [],
        "about": passport.get("about") or [],
        "benefits": passport.get("benefits") or [],
        "images": passport.get("images") or [],
        "videos": passport.get("videos") or [],
        "is_parent_company": passport.get("is_parent_company") if passport else None,
        "is_anonymous": passport.get("is_anonymous") if passport else bool(first.get("isAnonymous")),
        "has_passport": bool(passport.get("has_passport")),
        "jobs_count": passport.get("jobs_count") or to_int(meta.get("total")),
        "open_jobs_count": to_int(meta.get("total")),
    }
    facets = {f["name"]: f["options"] for f in filters(state, site_row)} if state else {}
    out["job_locations"] = [{"name": o["label"], "count": o["count"], "city_id": o["value"]} for o in facets.get("city", [])]
    out["job_disciplines"] = [{"name": o["label"], "count": o["count"], "discipline_id": o["value"]} for o in facets.get("discipline", [])]
    out["job_contract_types"] = [{"name": o["label"], "count": o["count"], "contract_type_id": o["value"]} for o in facets.get("contract_type", [])]
    return out


# ---- autosuggest ---------------------------------------------------------------------------------

def autosuggest(payload):
    """/searchbar/autosuggest/suggest -> [{title, type, frequency, company_id, city_id, parent_location}]."""
    out = []
    for item in as_list(g(payload, "itemCollection")):
        item = as_dict(item)
        title = text(item.get("title"))
        if not title:
            continue
        kind = text(item.get("type")) or ""
        out.append({
            "title": title,
            "type": SUGGESTION_TYPES.get(kind, kind.lower().replace("type_", "") or None),
            "frequency": to_int(item.get("frequency")),
            "company_id": to_int(item.get("companyId")),
            "city_id": to_int(item.get("cityId")),
            "parent_location": text(item.get("parentLocation")),
        })
    return out


__all__ = ["extract_app", "json_ld", "search_results", "filters", "job_item", "job_details", "company_details",
           "company_passport", "similar_jobs", "related_links", "autosuggest", "salary_from_text",
           "salary_from_unified", "clean_link", "list_items", "multiline"]
