"""Cache TTL per /stepstone/* endpoint (cache.py, keyed on the validated
params — marshmallow fills the defaults, so an omitted param and its default
share a row).

Result lists churn hourly (new postings, expiries, predicted-salary
refreshes); a single job changes rarely once posted; company passports move
daily at most; autosuggest frequencies drift slowly."""
from datetime import timedelta

SEARCH_CACHE = timedelta(hours=1)
FILTERS_CACHE = timedelta(hours=1)
LISTING_CACHE = timedelta(hours=1)
JOB_CACHE = timedelta(hours=6)
COMPANY_CACHE = timedelta(days=1)
COMPANY_JOBS_CACHE = timedelta(hours=1)
AUTOCOMPLETE_CACHE = timedelta(days=7)
