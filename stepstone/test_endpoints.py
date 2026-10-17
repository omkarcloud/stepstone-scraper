"""Live endpoint smoke tests: one call per /stepstone/* route against a running
service, with example values proven to return data (2026-10-17). The
listing tooling (derive_facts.py) reads each route's FIRST call from this
file's AST as its working example, so the values stay literals.

Skipped unless STEPSTONE_BASE points at a running service:

    SCRAPERS_PORT=6061 ONLY_SCRAPER=stepstone python run.py
    STEPSTONE_BASE=http://127.0.0.1:6061 python -m pytest stepstone/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("STEPSTONE_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set STEPSTONE_BASE to run live endpoint tests")


def call(path, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=300)
    assert resp.status_code == 200, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


def status(path, **params):
    from curl_cffi import requests
    return requests.get(BASE + path, params=params, timeout=300).status_code


def test_job_search():
    body = call("/stepstone/jobs/search", keyword="python developer", location="Berlin")
    assert body["count"] >= 25 and len(body["jobs"]) == 25 and body["total_pages"] >= 2 and body["next"]
    job = body["jobs"][0]
    assert job["id"] and job["title"] and job["link"].startswith("https://www.stepstone.de/") and job["company"]["name"]
    assert body["search"]["site"] == "de" and body["search"]["radius"] == 30
    page2 = call("/stepstone/jobs/search", keyword="python developer", location="Berlin", page=2)
    assert page2["current_page"] == 2 and page2["jobs"] and page2["jobs"][0]["id"] not in {j["id"] for j in body["jobs"]}
    body = call("/stepstone/jobs/search", keyword="software engineer", location="München", radius=50, posted_within="7_days",
                remote="partial", sort="date")
    assert body["jobs"] and body["search"]["filters"] == {"posted_within": "7_days", "remote": "partial"}
    body = call("/stepstone/jobs/search", keyword="nurse", location="London", site="totaljobs", posted_within="14_days", salary_min=30000)
    assert body["jobs"] and body["jobs"][0]["link"].startswith("https://www.totaljobs.com/job/") and body["search"]["radius_unit"] == "miles"
    body = call("/stepstone/jobs/search", keyword="data analyst", site="pnet")
    assert body["jobs"] and body["jobs"][0]["site"] == "pnet"
    assert call("/stepstone/jobs/search", location="Wien", site="at")["jobs"]
    assert status("/stepstone/jobs/search", site="de") == 400
    assert status("/stepstone/jobs/search", keyword="python", posted_within="3_days") == 400
    assert status("/stepstone/jobs/search", keyword="python", site="indeed") == 400


def test_job_filters():
    body = call("/stepstone/jobs/filters", keyword="python developer", location="Berlin")
    assert body["total_count"] >= 25 and body["filters"]
    names = {f["name"] for f in body["filters"]}
    assert {"posted_within", "remote", "salary_min", "skill", "discipline", "city"} <= names
    skill = next(f for f in body["filters"] if f["name"] == "skill")["options"][0]
    assert skill["value"] and skill["count"] >= 1
    narrowed = call("/stepstone/jobs/search", keyword="python developer", location="Berlin", skill=skill["value"])
    assert narrowed["search"]["filters"]["skill"] == skill["value"] and narrowed["count"] is not None


def test_job_details_and_batch():
    found = call("/stepstone/jobs/search", keyword="python developer", location="Berlin")["jobs"]
    job_id, link = found[0]["id"], found[0]["link"]
    body = call("/stepstone/jobs/details", job=link)
    assert body["id"] == job_id and body["title"] and body["description"]["text"] and body["sections"]
    assert body["company"]["name"] and body["posted_at"] and body["is_online"] is True
    assert call("/stepstone/jobs/details", job=job_id)["id"] == job_id
    body = call("/stepstone/jobs/batch", jobs=f"{job_id},{found[1]['id']},1")
    assert [j["id"] for j in body["jobs"]] == [job_id, found[1]["id"]] and body["not_found"] == [1]
    uk = call("/stepstone/jobs/search", keyword="python developer", location="London", site="totaljobs")["jobs"][0]
    body = call("/stepstone/jobs/details", job=uk["link"])
    assert body["id"] == uk["id"] and body["site"] == "totaljobs" and body["location"]["country"] == "GB"
    assert status("/stepstone/jobs/details", job="1") == 404
    assert status("/stepstone/jobs/details", job="not a job") == 400


def test_jobs_by_link():
    body = call("/stepstone/jobs/by-link", link="https://www.stepstone.de/jobs/python-developer/in-berlin?radius=30&ag=age_7")
    assert body["jobs"] and body["search"]["filters"] == {"posted_within": "7_days"} and body["filters"]
    body = call("/stepstone/jobs/by-link", link="https://www.stepstone.de/cmp/de/ratbacher-gmbh-46710/jobs", page=2)
    assert body["current_page"] == 2 and body["jobs"] and all(j["company"]["id"] == 46710 for j in body["jobs"])
    assert status("/stepstone/jobs/by-link", link="https://www.stepstone.de/stellenangebote--x--13818030-inline.html") == 400


def test_companies():
    body = call("/stepstone/companies/details", company="https://www.stepstone.de/cmp/de/ratbacher-gmbh-46710/jobs")
    assert body["id"] == 46710 and body["name"] == "Ratbacher GmbH" and body["address"] and body["about"] and body["open_jobs_count"]
    assert call("/stepstone/companies/details", company="46710")["name"] == "Ratbacher GmbH"
    body = call("/stepstone/companies/jobs", company="ratbacher-gmbh-46710", sort="date")
    assert body["count"] >= 25 and len(body["jobs"]) == 25 and all(j["company"]["id"] == 46710 for j in body["jobs"])
    assert body["company"]["name"] == "Ratbacher GmbH" and body["filters"]
    # UK brands: the company page is /jobs/<slug>?cmpId=<id>; agencies often list nothing there
    uk = next(j["company"] for j in call("/stepstone/jobs/search", keyword="developer", location="London", site="totaljobs")["jobs"]
              if j["company"]["id"])
    body = call("/stepstone/companies/jobs", company=uk["link"])
    assert body["company"]["id"] == uk["id"] and body["search"]["filters"]["company"] == str(uk["id"])
    assert all(j["company"]["id"] == uk["id"] for j in body["jobs"])
    assert call("/stepstone/companies/details", company=uk["id"], site="totaljobs")["id"] == uk["id"]
    assert status("/stepstone/companies/details", company="Google") == 400


def test_autocomplete_and_sites():
    body = call("/stepstone/autocomplete", query="pyth")
    assert body["suggestions"] and body["suggestions"][0]["type"] == "job_title" and body["suggestions"][0]["frequency"] >= 1
    assert call("/stepstone/autocomplete", query="nurse", site="totaljobs", limit=5)["suggestions"]
    sites = call("/stepstone/sites")["sites"]
    assert [s["site"] for s in sites][:4] == ["de", "at", "be", "nl"] and len(sites) == 8
