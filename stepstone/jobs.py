"""/stepstone/jobs/* — job search with the boards' own filters, the filter
facets with counts, a job by id / link, a batch of jobs and "any listing
page by link", all from the HTML pages' embedded state (stepstone/parsers.py).

A search page is <search_prefix>/<keyword-slug>/in-<location-slug>?radius=…
&page=N plus one query param per filter. The two filter families map the
public params differently (FAMILY_PARAMS): the stepstone.* boards and Pnet
share one set (ag / wfh / us / wt / di / ct / ex / fsk / fu / se / fci / fre
/ am / fdl), the UK brands another (postedWithin / wfh / salary /
tjg_job_type_id / companytypes / fsk). A param a family has no facet for is
a 400, not a silent no-op. Pages hold 25 jobs (the site ignores `limit`) and
stop at 5,000 results.
"""
from stepstone import fetch
from stepstone import parsers as P
from stepstone import sites as S

PER_PAGE = 25
MAX_PAGES = 200

# public param -> (url key, value map or None) per filter family
FAMILY_PARAMS = {
    "stepstone": {
        "posted_within": ("ag", {"1_day": "age_1", "7_days": "age_7"}),
        "remote": ("wfh", {"full": "1", "partial": "2"}),
        "salary_min": ("us", None),
        "work_type": ("wt", {"full_time": "80001", "part_time": "80002"}),
        "application_method": ("am", {"quick_apply": "INTERNAL", "company_website": "EXTERNAL"}),
        "language": ("fdl", None),
        "discipline": ("di", None), "category": ("fu", None), "sector": ("se", None), "skill": ("fsk", None),
        "city": ("fci", None), "region": ("fre", None), "contract_type": ("ct", None), "experience": ("ex", None),
    },
    "uk": {
        "posted_within": ("postedWithin", {"1_day": "1", "3_days": "3", "7_days": "7", "14_days": "14"}),
        "remote": ("wfh", {"full": "1", "partial": "1"}),
        "salary_min": ("salary", None),
        "skill": ("fsk", None), "job_type": ("tjg_job_type_id", None), "company_type": ("companytypes", None),
    },
}
FILTER_KEYS = ("posted_within", "remote", "salary_min", "work_type", "application_method", "language", "discipline",
               "category", "sector", "skill", "city", "region", "contract_type", "experience", "job_type",
               "company_type")


def filter_params(site_row, filters):
    """{public param: value} -> the board's URL params; ValueError for a
    filter the board's family does not have."""
    table = FAMILY_PARAMS[site_row["family"]]
    out = {}
    for name, value in filters.items():
        if value in (None, "", []):
            continue
        if name not in table:
            raise ValueError(f"{name} is not a filter on {site_row['name']}; it offers: "
                             f"{', '.join(k for k in FILTER_KEYS if k in table)}.")
        key, mapping = table[name]
        if mapping is not None:
            if str(value) not in mapping:
                raise ValueError(f"{name}={value} is not available on {site_row['name']}; choose one of: "
                                 f"{', '.join(mapping)}.")
            value = mapping[str(value)]
        out[key] = value
    if "salary" in out and site_row["family"] == "uk":
        out["salarytypedesc"] = "Annum"
    return out


def _sort_params(sort):
    if sort == "date":
        return {"orderByField": "date", "action": "sort_publish"}
    return {}


def _result_list(site_row, path, params, label):
    resp = fetch.page(site_row, path, params, label=label)
    state = P.extract_app(resp.text, "app-unifiedResultlist")
    if not state:
        raise fetch.StepstoneUpstreamError(f"no result list in the page for {label} (landed on {resp.url})")
    return state


def _finish(out, page):
    pagination = out["pagination"]
    pagination["page"] = page
    pagination["items_per_page"] = PER_PAGE
    if pagination.get("total_pages") is not None:
        pagination["total_pages"] = min(pagination["total_pages"], MAX_PAGES)
    return out


def _split(kwargs):
    return {k: kwargs.pop(k, None) for k in FILTER_KEYS}


def search(keyword=None, location=None, site="de", radius=None, sort="relevance", page=1, **filters):
    """One page (25 jobs) of a job search on one board."""
    row = S.site(site)
    params = {"radius": radius, "page": page if page > 1 else None, **_sort_params(sort), **filter_params(row, filters)}
    path = S.search_path(row, keyword, location)
    state = _result_list(row, path, params, f"job search {keyword!r} in {location!r} on {row['host']}")
    out = P.search_results(state, row)
    out["search"]["sort"] = sort
    return _finish(out, page)


def filters(keyword=None, location=None, site="de", radius=None, **filters):
    """The facets of a search with their option values and counts (posting
    age, remote, salary floors, work / contract type, experience, skills,
    disciplines, categories, sectors, cities, regions, application method,
    language on the stepstone boards; posting age, remote, job and company
    types, skills on the UK brands)."""
    row = S.site(site)
    params = {"radius": radius, **filter_params(row, filters)}
    path = S.search_path(row, keyword, location)
    state = _result_list(row, path, params, f"job filters {keyword!r} in {location!r} on {row['host']}")
    out = P.search_results(state, row)
    return {"search": out["search"], "total_count": out["counts"]["total"], "counts": out["counts"],
            "filters": P.filters(state, row), "related_searches": out["related_searches"]}


def _details(job, default_site):
    row = S.site(job.get("site") or default_site)
    path = S.job_path(row, job["id"])
    resp = fetch.page(row, path, label=f"job {job['id']} on {row['host']}")
    data = P.job_details(resp.text, row, job["id"])
    if not data:
        raise fetch.StepstoneNotFound(f"{row['name']} has no job {job['id']} (expired or removed)")
    return data


def details(job, site="de"):
    """One job by id or link: full description (sections, text and HTML),
    contract / work type, salary (stated or StepStone's estimate), location
    with coordinates, the employer's card with rating and passport, similar
    jobs and related searches."""
    return _details(job, site)


def batch(jobs, site="de"):
    """Up to 10 jobs in parallel; expired ids are listed in not_found,
    other failures in errors."""
    results = fetch.run_parallel([lambda j=j: _details(j, site) for j in jobs])
    found, not_found, errors = [], [], []
    for job, (status, value) in zip(jobs, results):
        if status == "ok":
            found.append(value)
        elif isinstance(value, fetch.StepstoneNotFound):
            not_found.append(job["id"])
        else:
            errors.append({"id": job["id"], "error": str(value)})
    if not found and errors and not not_found:
        raise fetch.StepstoneUpstreamError("; ".join(e["error"] for e in errors))
    return {"jobs": found, "not_found": not_found, "errors": errors}


def by_link(link, page=1):
    """The jobs of any search or company-jobs page link on a board (saved
    searches with the site's own filters), page by page, with its facets."""
    row = S.site(link["site"])
    params = {k: v for k, v in link["query"].items()}
    if page > 1:
        params["page"] = page
    resp = fetch.page(row, link["path"], params, label=f"listing {link['path']} on {row['host']}")
    state = P.extract_app(resp.text, "app-unifiedResultlist")
    if not state:
        raise fetch.StepstoneBadRequest(f"{link['path']} is not a job listing page on {row['name']}")
    out = P.search_results(state, row)
    out["filters"] = P.filters(state, row)
    return _finish(out, page)
