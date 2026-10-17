"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json. Set STEPSTONE_PROXY first (see config.py).
"""
import json
import os

from stepstone import companies, jobs, refs

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # a job id or any job link from a supported board
    save("job_14448261.json", jobs.details(refs.resolve_job("14448261")))

    # 25 jobs per page, with every site filter
    save("search_software_engineer_berlin.json", jobs.search(keyword="software engineer", location="Berlin"))

    # an employer's profile: a company id, its slug-id or a company link
    save("company_bmw_group.json", companies.details(refs.resolve_company("bmw-group-27361")))
