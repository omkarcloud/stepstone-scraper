"""/stepstone/companies/* — a company's profile (the "company passport" the
stepstone boards show: address, size, founding year, industries, about
sections, benefits, media) and its open jobs, both from the company jobs
page (/cmp/<lang>/<slug>-<id>/jobs on the stepstone boards, /jobs/<slug>?cmpId=<id>
on the UK brands). The page accepts the same filter and sort params as a
search, so the jobs route takes them too.
"""
from stepstone import fetch
from stepstone import jobs as J
from stepstone import parsers as P
from stepstone import sites as S


def _row(company, default_site):
    return S.site(company.get("site") or default_site)


def details(company, site="de"):
    """One company by id, slug-id or link: profile passport (when the board
    has one), logo, hero image, open-jobs count and where / in which fields
    those jobs are."""
    row = _row(company, site)
    path = S.company_path(row, company["id"], company.get("slug"))
    resp = fetch.page(row, path, label=f"company {company['id']} on {row['host']}")
    data = P.company_details(resp.text, row, company["id"])
    if not data or not data.get("name"):
        raise fetch.StepstoneNotFound(f"{row['name']} has no company {company['id']}")
    return data


def jobs(company, site="de", radius=None, sort="relevance", page=1, **filters):
    """One page (25) of a company's open jobs, with the search filters."""
    row = _row(company, site)
    params = {"radius": radius, "page": page if page > 1 else None, **J._sort_params(sort),
              **J.filter_params(row, filters)}
    path = S.company_path(row, company["id"], company.get("slug"))
    state = J._result_list(row, path, params, f"company {company['id']} jobs on {row['host']}")
    out = P.search_results(state, row)
    out["search"]["sort"] = sort
    first = next((j for j in out["jobs"] if j["company"].get("id")), None)
    out["company"] = {"id": company["id"], "name": (first or {}).get("company", {}).get("name")
                      or out["search"].get("keyword"), "site": row["code"],
                      "link": P.clean_link(S.link(row, path), row)}
    out["filters"] = P.filters(state, row)
    return J._finish(out, page)
