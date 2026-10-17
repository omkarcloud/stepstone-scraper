"""/stepstone/autocomplete and /stepstone/sites."""
from stepstone import fetch
from stepstone import parsers as P
from stepstone import sites as S

AUTOSUGGEST_PATH = "/searchbar/autosuggest/suggest"


def autocomplete(query, site="de", limit=10):
    """The search box's suggestions for a partial keyword: job titles (with
    how many live postings carry them), companies and locations when the
    board returns them."""
    row = S.site(site)
    payload = fetch.get_json(row, AUTOSUGGEST_PATH, {"Text": query, "SiteId": fetch.site_id(row)},
                             label=f"autosuggest {query!r} on {row['host']}")
    return {"query": query, "site": row["code"], "suggestions": P.autosuggest(payload)[:limit]}


def sites():
    """Every StepStone Group board this API serves (valid `site` values)."""
    return {"sites": S.public_rows()}
