"""Marshmallow request schemas for every /stepstone/* route.

Generic fields live in the shared top-level schema_fields.py; this module
adds the StepStone resolvers (refs.py). Every schema's load() output is the
kwargs dict its endpoint function takes.

ONE param per input (tripadvisor QueryOrLinkField convention, never a
sibling `url`/`id` pair): `job` takes a job id OR any board's job link;
`company` a company id, 'slug-id' or a company link; `link` a search or
company-jobs page link; `site` a board code, an ISO country or a host /
link on the board. A job / company link names its board, so `site` is only
the default for bare ids.

Filter ids (`discipline`, `skill`, `city`, …) are the values
/stepstone/jobs/filters lists for the same search.
"""
import re

from marshmallow import ValidationError, fields, validate, validates_schema

from schema_fields import (BaseSchema, ChoiceField, CommaListField, LanguageCodeField, PageField, PositiveInt,
                           QueryField, RefField, StrippedString)
from stepstone import refs

MAX_BATCH = 10
MAX_PAGE = 200          # StepStone stops at 5,000 results = 200 pages of 25
_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,24}$")


class SiteField(RefField):
    """The board: code (de, at, be, nl, totaljobs, cwjobs, nijobs, pnet), ISO country, host or link. Default de."""
    resolver = staticmethod(refs.resolve_site)

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", "de")
        super().__init__(**kwargs)


class JobField(RefField):
    resolver = staticmethod(refs.resolve_job)


class CompanyField(RefField):
    resolver = staticmethod(refs.resolve_company)


class ListingLinkField(RefField):
    resolver = staticmethod(refs.resolve_listing)


class JobListField(fields.Field):
    """'13818030, https://www.stepstone.de/stellenangebote--…-inline.html' -> resolved jobs (1..MAX_BATCH)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", True)
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        try:
            return refs.resolve_jobs(value, MAX_BATCH)
        except ValueError as e:
            raise ValidationError(str(e))


class IdListField(CommaListField):
    """Filter option ids from /stepstone/jobs/filters ('2347,920488205')."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_items", 10)
        super().__init__(upper=False, **kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        items = super()._deserialize(value, attr, data, **kwargs)
        for item in items or []:
            if not _ID_RE.match(item):
                raise ValidationError(f"'{item}' is not a filter id (see /stepstone/jobs/filters).")
        return items


# ---- jobs ----------------------------------------------------------------------------------------

class _JobFilters(BaseSchema):
    site = SiteField()
    radius = fields.Integer(load_default=None, strict=False, validate=validate.Range(min=0, max=200))
    posted_within = ChoiceField(["1_day", "3_days", "7_days", "14_days"])
    remote = ChoiceField(["full", "partial"])
    salary_min = PositiveInt(max_value=10_000_000)
    work_type = ChoiceField(["full_time", "part_time"])
    application_method = ChoiceField(["quick_apply", "company_website"])
    language = LanguageCodeField(allow_locale=False)
    discipline = IdListField()
    category = IdListField()
    sector = IdListField()
    skill = IdListField()
    city = IdListField()
    region = IdListField()
    contract_type = IdListField()
    experience = IdListField()
    job_type = IdListField()
    company_type = IdListField()


class _KeywordLocation(_JobFilters):
    keyword = StrippedString(required=False, load_default=None, validate=validate.Length(max=200))
    location = StrippedString(required=False, load_default=None, validate=validate.Length(max=150))

    @validates_schema
    def _needs_a_query(self, data, **kwargs):
        if not data.get("keyword") and not data.get("location"):
            raise ValidationError("Give a keyword, a location, or both.", "keyword")


class JobSearchSchema(_KeywordLocation):
    sort = ChoiceField(["relevance", "date"], load_default="relevance")
    page = PageField(max_page=MAX_PAGE)


class JobFiltersSchema(_KeywordLocation):
    pass


class JobSchema(BaseSchema):
    job = JobField()
    site = SiteField()


class JobBatchSchema(BaseSchema):
    jobs = JobListField()
    site = SiteField()


class ListingSchema(BaseSchema):
    link = ListingLinkField()
    page = PageField(max_page=MAX_PAGE)


# ---- companies ---------------------------------------------------------------------------------

class CompanySchema(BaseSchema):
    company = CompanyField()
    site = SiteField()


class CompanyJobsSchema(_JobFilters):
    company = CompanyField()
    sort = ChoiceField(["relevance", "date"], load_default="relevance")
    page = PageField(max_page=MAX_PAGE)


# ---- autocomplete / sites ----------------------------------------------------------------------

class AutocompleteSchema(BaseSchema):
    query = QueryField(max_length=100)
    site = SiteField()
    limit = fields.Integer(load_default=10, strict=False, validate=validate.Range(min=1, max=20))


class EmptySchema(BaseSchema):
    pass
