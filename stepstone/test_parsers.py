"""Parser tests on saved pages (stepstone/fixtures/*.html.gz, fetched
2026-10-17 through residential exits) and on the input resolvers. No network.

    /usr/local/bin/python3.12 -m pytest stepstone/test_parsers.py -q
"""
import gzip
import json
import os

import pytest

from stepstone import parsers as P
from stepstone import refs
from stepstone import sites as S
from stepstone.jobs import filter_params

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def html(name):
    with gzip.open(os.path.join(FIXTURES, name + ".html.gz"), "rt", encoding="utf-8") as f:
        return f.read()


def state(name):
    out = P.extract_app(html(name), "app-unifiedResultlist")
    assert out, f"{name}: no result list state"
    return out


# ---- result lists --------------------------------------------------------------------------------

def test_search_de():
    row = S.site("de")
    out = P.search_results(state("search_de"), row)
    assert out["search"]["keyword"] == "Python Developer" and out["search"]["location"] == "Berlin"
    assert out["search"]["radius"] == 30 and out["search"]["radius_unit"] == "km"
    assert out["pagination"]["items_per_page"] == 25 and out["pagination"]["total_count"] >= 25
    assert out["pagination"]["total_pages"] >= 1 and len(out["jobs"]) == 25
    job = out["jobs"][0]
    assert job["id"] and job["title"] and job["link"].startswith("https://www.stepstone.de/stellenangebote--")
    assert "rltr" not in job["link"] and job["site"] == "de"
    assert job["company"]["name"] and job["company"]["link"].startswith("https://www.stepstone.de/cmp/de/")
    assert job["posted_at"] and job["posted_at"].endswith("Z") and job["expires_at"]
    assert job["match_type"] in ("direct", "related", "nearby", "related_company", "recommended")
    with_salary = [j for j in out["jobs"] if j["salary"]]
    assert with_salary and with_salary[0]["salary"]["currency"] == "EUR" and with_salary[0]["salary"]["period"] == "year"
    assert any(j["salary"]["is_predicted"] for j in with_salary)
    assert any(j["remote"] in ("partial", "full") for j in out["jobs"])
    assert any(j["benefits"] for j in out["jobs"]) and all(isinstance(j["benefits"], list) for j in out["jobs"])
    assert out["counts"]["total"] == out["pagination"]["total_count"] and out["counts"]["direct"] is not None
    assert out["related_searches"] and out["related_searches"][0]["title"]
    assert out["related_skills"] and out["related_skills"][0]["link"].startswith("https://www.stepstone.de/")
    assert out["breadcrumbs"][0]["label"] == "Jobs"
    for job in out["jobs"]:
        assert "<" not in (job["snippet"] or "")


def test_filters_de():
    row = S.site("de")
    facets = P.filters(state("search_de"), row)
    names = [f["name"] for f in facets]
    for expected in ("posted_within", "salary_min", "remote", "work_type", "discipline", "contract_type", "experience",
                     "skill", "category", "sector", "city", "region", "application_method", "language"):
        assert expected in names, expected
    by_name = {f["name"]: f for f in facets}
    assert {o["value"] for o in by_name["posted_within"]["options"]} <= {"1_day", "7_days"}
    assert {o["value"] for o in by_name["remote"]["options"]} <= {"full", "partial", "none"}
    assert {o["value"] for o in by_name["work_type"]["options"]} == {"full_time", "part_time"}
    assert all(isinstance(o["value"], int) for o in by_name["salary_min"]["options"])
    skill = by_name["skill"]["options"][0]
    assert skill["value"].isdigit() and skill["label"] and skill["count"] >= 1 and skill["is_selected"] is False
    assert by_name["skill"]["label"] == "Fähigkeiten"
    # filter params the search builds from those values
    assert filter_params(row, {"posted_within": "7_days", "remote": "partial", "skill": ["657911"], "salary_min": 60000}) == \
        {"ag": "age_7", "wfh": "2", "fsk": ["657911"], "us": 60000}
    with pytest.raises(ValueError):
        filter_params(row, {"posted_within": "3_days"})
    with pytest.raises(ValueError):
        filter_params(row, {"job_type": ["10"]})


def test_search_uk_and_others():
    tj = P.search_results(state("search_tj"), S.site("totaljobs"))
    assert tj["search"]["radius_unit"] == "miles" and tj["jobs"][0]["link"].startswith("https://www.totaljobs.com/job/")
    assert tj["jobs"][0]["site"] == "totaljobs"
    salaries = [j["salary"] for j in tj["jobs"] if j["salary"]]
    assert salaries and salaries[0]["currency"] == "GBP" and salaries[0]["text"]
    assert any(s["max"] or s["min"] or s["exact"] for s in salaries)
    facets = {f["name"]: f for f in P.filters(state("search_tj"), S.site("totaljobs"))}
    assert {o["value"] for o in facets["posted_within"]["options"]} == {"1_day", "3_days", "7_days", "14_days"}
    assert "job_type" in facets and "company_type" in facets
    assert filter_params(S.site("totaljobs"), {"posted_within": "3_days", "salary_min": 50000, "remote": "full"}) == \
        {"postedWithin": "3", "salary": 50000, "wfh": "1", "salarytypedesc": "Annum"}
    with pytest.raises(ValueError):
        filter_params(S.site("totaljobs"), {"discipline": ["2347"]})
    be = P.search_results(state("search_be"), S.site("be"))
    assert be["jobs"] and be["jobs"][0]["link"].startswith("https://www.stepstone.be/vacatures--")
    pnet = P.search_results(state("search_pnet"), S.site("pnet"))
    assert pnet["jobs"] and pnet["jobs"][0]["link"].startswith("https://www.pnet.co.za/jobs--")
    assert pnet["jobs"][0]["company"]["link"].startswith("https://www.pnet.co.za/cmp/en/")
    at = P.search_results(state("search_at"), S.site("at"))
    assert at["jobs"] and at["jobs"][0]["site"] == "at"


# ---- job pages ------------------------------------------------------------------------------------

def test_job_details_de():
    row = S.site("de")
    job = P.job_details(html("job_de"), row)
    assert job["id"] == 13818030 and job["global_id"] and job["site"] == "de"
    assert job["title"].startswith("Backend Entwickler Python")
    assert job["link"] == ("https://www.stepstone.de/stellenangebote--Backend-Entwickler-Python-m-w-d-Data-Engineer-Python-"
                           "Berlin-Mannheim-Frankfurt-Stuttgart-Karlsruhe-Ratbacher-GmbH--13818030-inline.html")
    assert job["is_online"] is True and job["is_expired"] is False
    assert job["posted_at"] == "2026-09-22T11:03:04Z" and job["expires_at"] == "2027-03-21T11:30:13Z"
    assert job["employment_type"] == "FULL_TIME" and job["contract_type"] == "Feste Anstellung"
    assert job["work_types"] == ["Homeoffice möglich", "Vollzeit"] and job["remote"] == "partial"
    assert job["industry"] == "IT, IT-Softwareentwicklung" and job["is_direct_apply"] is True
    assert job["location"]["text"].startswith("Berlin") and job["location"]["city"] == "Berlin" and job["location"]["country"] == "DE"
    assert job["salary"] == {"text": None, "min": 53000, "max": 74000, "exact": None, "currency": "EUR",
                             "period": "year", "is_predicted": True, "wage_type": "brutto"}
    assert job["badges"] == ["Anschreiben nicht erforderlich"]
    names = [s["name"] for s in job["sections"]]
    assert names == ["introduction", "description", "profile", "benefits", "additionalInformation"]
    assert job["sections"][1]["title"] == "Deine Aufgaben" and job["sections"][1]["text"] and "<" not in job["sections"][1]["text"]
    assert job["description"]["text"] and "<strong>" in job["description"]["html"] and "<" not in job["description"]["text"]
    assert job["hero_image_link"].endswith(".jpg") and job["videos"] == ["https://www.youtube.com/watch?v=LhoAYmIeo20"]
    company = job["company"]
    assert company["id"] == 46710 and company["name"] == "Ratbacher GmbH" and company["employees"] == "51-250"
    assert company["link"].startswith("https://www.stepstone.de/cmp/de/") and company["logo_link"].endswith(".gif")
    assert company["rating"] is None and company["sectors"] == ["Personaldienstleistungen"]
    assert company["address"] == "Kronprinzstraße 28, Stuttgart" and company["founded"] == 2006
    assert company["industries"] == ["Personaldienstleistungen"] and company["jobs_count"] == 220
    assert company["about"][0]["title"] == "About Us" and company["about"][0]["text"].startswith("Ratbacher GmbH")
    assert len(company["images"]) == 4 and company["has_passport"] is True
    assert len(job["similar_jobs"]) == 10
    similar = job["similar_jobs"][0]
    assert similar["id"] == 13850318 and similar["link"].startswith("https://www.stepstone.de/stellenangebote--")
    assert similar["company"]["name"] == "inovex GmbH" and similar["remote"] == "partial" and similar["posted_at"]
    assert job["related_searches"][0] == {"title": "Backendentwickler/In Jobs", "link": "https://www.stepstone.de/jobs/backendentwickler",
                                          "type": "job_title"}


def test_job_details_uk():
    job = P.job_details(html("job_tj"), S.site("totaljobs"))
    assert job["id"] == 108167578 and job["title"] == "Python Developer" and job["site"] == "totaljobs"
    assert job["link"] == "https://www.totaljobs.com/job/python-developer/anson-mccade-job108167578"
    assert job["contract_type"] == "Permanent Contract; Hybrid" and job["remote"] == "partial"
    assert job["salary"] == {"text": "Up to £80,000 per annum", "min": None, "max": 80000, "exact": None,
                             "currency": "GBP", "period": "year", "is_predicted": False}
    assert job["location"]["latitude"] == pytest.approx(51.5213089) and job["location"]["city"] == "Central London"
    assert job["location"]["region"] == "South East" and job["location"]["country"] == "GB"
    assert job["sections"][0]["name"] == "jobDescription" and len(job["description"]["text"]) > 500
    assert job["company"]["id"] == 1231308 and job["company"]["name"] == "Anson Mccade"
    assert job["company"]["link"] == "https://www.totaljobs.com/jobs/anson-mccade?cmpId=1231308"
    assert job["company"]["sectors"] == ["Other Sectors and Industries"] and job["company"]["has_passport"] is False


def test_job_details_missing():
    assert P.job_details(html("search_de"), S.site("de")) is None
    assert P.job_details("<html></html>", S.site("de")) is None


# ---- company pages ---------------------------------------------------------------------------------

def test_company_de():
    company = P.company_details(html("company_de"), S.site("de"))
    assert company["id"] == 46710 and company["name"] == "Ratbacher GmbH" and company["site"] == "de"
    assert company["link"] == "https://www.stepstone.de/cmp/de/ratbacher-gmbh-46710/jobs"
    assert company["address"] and company["founded"] == 2006 and company["employees"] == "51-250"
    assert company["about"] and company["images"] and company["has_passport"] is True
    assert company["open_jobs_count"] >= 50 and company["jobs_count"] >= company["open_jobs_count"]
    assert company["job_locations"] and company["job_locations"][0]["count"] >= 1
    assert company["job_disciplines"]


def test_company_uk():
    company = P.company_details(html("company_tj"), S.site("totaljobs"))
    # the saved page is the company's London-only listing (no items): the id comes from the link
    assert company["id"] == 1231308 and company["name"] == "Anson Mccade" and company["has_passport"] is False
    assert company["link"].startswith("https://www.totaljobs.com/jobs/anson-mccade") and company["open_jobs_count"] is not None
    assert company["about"] == [] and company["job_locations"] == []


# ---- autosuggest / salary text ------------------------------------------------------------------------

def test_autosuggest():
    with open(os.path.join(FIXTURES, "autosuggest_de.json"), encoding="utf-8") as f:
        rows = P.autosuggest(json.load(f))
    assert rows[0]["title"] == "Python-Entwickler/in" and rows[0]["type"] == "job_title" and rows[0]["frequency"] >= 1


def test_salary_text():
    gb = S.site("totaljobs")
    assert P.salary_from_text("£50,000 - £60,000 per annum", gb) == {"text": "£50,000 - £60,000 per annum", "min": 50000, "max": 60000,
                                                                   "exact": None, "currency": "GBP", "period": "year", "is_predicted": False}
    assert P.salary_from_text("£14.50 per hour", gb)["exact"] == 14.5 and P.salary_from_text("£14.50 per hour", gb)["period"] == "hour"
    assert P.salary_from_text("From £30,000", gb)["min"] == 30000
    assert P.salary_from_text("Competitive", gb)["min"] is None
    assert P.salary_from_text("", gb) is None
    de = S.site("de")
    assert P.salary_from_text("45.000 - 55.000 € pro Jahr", de) == {"text": "45.000 - 55.000 € pro Jahr", "min": 45000, "max": 55000,
                                                                   "exact": None, "currency": "EUR", "period": "year", "is_predicted": False}


# ---- resolvers -----------------------------------------------------------------------------------------

def test_resolve_site():
    assert refs.resolve_site("") == "de" and refs.resolve_site("AT") == "at" and refs.resolve_site("uk") == "totaljobs"
    assert refs.resolve_site("https://www.pnet.co.za/jobs/x") == "pnet" and refs.resolve_site("stepstone.be") == "be"
    with pytest.raises(ValueError):
        refs.resolve_site("indeed")


def test_resolve_job():
    assert refs.resolve_job("13818030") == {"id": 13818030, "site": None}
    link = "https://www.stepstone.de/stellenangebote--Backend-Entwickler--13818030-inline.html?rltr=1_1"
    assert refs.resolve_job(link) == {"id": 13818030, "site": "de"}
    assert refs.resolve_job("https://www.totaljobs.com/job/python-developer/anson-mccade-job108167578") == {"id": 108167578, "site": "totaljobs"}
    assert refs.resolve_job("www.pnet.co.za/jobs--Full-Stack-Developer--4287198-inline.html") == {"id": 4287198, "site": "pnet"}
    assert refs.resolve_jobs("13818030, 13818030, https://www.stepstone.at/stellenangebote--x--990164-inline.html") == \
        [{"id": 13818030, "site": None}, {"id": 990164, "site": "at"}]
    for bad in ("not a job", "https://www.indeed.com/viewjob?jk=abc", "https://www.stepstone.de/jobs/python"):
        with pytest.raises(ValueError):
            refs.resolve_job(bad)
    with pytest.raises(ValueError):
        refs.resolve_jobs(",".join(str(1000 + i) for i in range(11)))


def test_resolve_company():
    assert refs.resolve_company("46710") == {"id": 46710, "slug": None, "site": None}
    assert refs.resolve_company("ratbacher-gmbh-46710") == {"id": 46710, "slug": "ratbacher-gmbh", "site": None}
    assert refs.resolve_company("https://www.stepstone.de/cmp/de/ratbacher-gmbh-46710/jobs") == {"id": 46710, "slug": "ratbacher-gmbh", "site": "de"}
    assert refs.resolve_company("https://www.stepstone.de/cmp/de/Ratbacher-GmbH-46710/jobs.html") == {"id": 46710, "slug": "ratbacher-gmbh", "site": "de"}
    assert refs.resolve_company("https://www.totaljobs.com/jobs/anson-mccade/in-london?cmpId=1231308&cmp=1") == \
        {"id": 1231308, "slug": "anson-mccade", "site": "totaljobs"}
    assert refs.resolve_company("https://www.stepstone.be/cmp/nl/talan-belgium-156927/vacatures") == {"id": 156927, "slug": "talan-belgium", "site": "be"}
    for bad in ("Google", "https://www.stepstone.de/jobs/python", "https://www.linkedin.com/company/acme"):
        with pytest.raises(ValueError):
            refs.resolve_company(bad)


def test_resolve_listing():
    out = refs.resolve_listing("https://www.stepstone.de/jobs/python-developer/in-berlin?radius=30&ag=age_7&page=3&rltr=x")
    assert out == {"site": "de", "path": "/jobs/python-developer/in-berlin", "query": {"ag": ["age_7"], "radius": ["30"]}}
    assert refs.resolve_listing("https://www.totaljobs.com/jobs/anson-mccade?cmpId=1231308&cmp=1")["query"] == {"cmp": ["1"], "cmpId": ["1231308"]}
    assert refs.resolve_listing("https://www.stepstone.be/cmp/nl/talan-belgium-156927/vacatures")["path"] == "/cmp/nl/talan-belgium-156927/vacatures"
    for bad in ("python", "https://www.stepstone.de/stellenangebote--x--13818030-inline.html", "https://www.stepstone.de/gehaltsplaner/"):
        with pytest.raises(ValueError):
            refs.resolve_listing(bad)


def test_paths():
    de = S.site("de")
    assert S.search_path(de, "Python Developer", "Berlin") == "/jobs/python-developer/in-berlin"
    assert S.search_path(de, "C# Entwickler", None) == "/jobs/c%23-entwickler"
    assert S.search_path(de, None, "München") == "/jobs/in-m%C3%BCnchen"
    assert S.job_path(de, 13818030) == "/stellenangebote--x--13818030-inline.html"
    assert S.job_path(S.site("totaljobs"), 108167578) == "/job/x/x-job108167578"
    assert S.company_path(de, 46710) == "/jobs?cmpId=46710&cmp=1"
    assert S.company_path(de, 46710, "ratbacher-gmbh") == "/cmp/de/ratbacher-gmbh-46710/jobs"
    assert S.company_path(S.site("totaljobs"), 1231308, "anson-mccade") == "/jobs/anson-mccade?cmpId=1231308&cmp=1"
