"""Value helpers for the StepStone parsers: safe nested access, numbers,
text, ISO dates and HTML -> text. Pure functions, no network; every helper
tolerates None and junk.

Same semantics as jobstreet/shared.py and seek/shared.py (g / as_list /
as_dict, num, to_int, rounded, text, multiline, iso_date, iso_datetime,
html_to_text); they live here so the stepstone package stands alone (it
ships as its own open-source repo, which must not pull in other sites'
modules). One difference: html_to_text keeps a list item's dash on its text
when the item wraps a paragraph (<li><p>…</p></li>, common on StepStone)."""
import html as html_lib
import re
from datetime import datetime
from html.parser import HTMLParser

# ---- safe access ---------------------------------------------------------------------------

def g(obj, *path, default=None):
    """Safe nested get: g(d, "a", "b", 0) -> d["a"]["b"][0] or default."""
    for step in path:
        if isinstance(step, int):
            if not isinstance(obj, list) or not -len(obj) <= step < len(obj):
                return default
            obj = obj[step]
        else:
            if not isinstance(obj, dict):
                return default
            obj = obj.get(step)
        if obj is None:
            return default
    return obj


def as_list(value):
    return value if isinstance(value, list) else []


def as_dict(value):
    return value if isinstance(value, dict) else {}


# ---- numbers -------------------------------------------------------------------------------

def num(value):
    """int / float / numeric string ('1,234.5') -> float; bool, junk, NaN -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    try:
        out = float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
    return out if out == out else None


def to_int(value):
    out = num(value)
    return int(round(out)) if out is not None else None


def rounded(value, digits=2):
    out = num(value)
    return round(out, digits) if out is not None else None


# ---- text / dates --------------------------------------------------------------------------

def text(value):
    """Whitespace-collapsed, HTML-unescaped string; '' -> None. Accepts
    {"text": ...} wrappers."""
    if isinstance(value, dict):
        value = value.get("text")
    if value is None:
        return None
    out = " ".join(html_lib.unescape(str(value)).replace("\xa0", " ").replace(" ", " ").split())
    return out or None


def multiline(value):
    """Like text() but keeps line breaks (review bodies, descriptions)."""
    if isinstance(value, dict):
        value = value.get("text")
    if value is None:
        return None
    lines = [" ".join(line.split()) for line in html_lib.unescape(str(value)).replace("\xa0", " ").splitlines()]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out or None


_LONG_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y")


def iso_date(value):
    """'21 Sep 2026' / '2026-09-22T…' / 'September 22, 2026' -> 'YYYY-MM-DD'."""
    value = text(value)
    if not value:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return "-".join(match.groups())
    for fmt in _LONG_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def iso_datetime(value):
    """'2026-09-18T04:30:49.433Z' / {dateTimeUtc} -> '2026-09-18T04:30:49Z' (UTC, seconds)."""
    if isinstance(value, dict):
        value = value.get("dateTimeUtc") or value.get("dateTime") or value.get("shortAbsoluteLabel")
    value = text(value)
    if not value:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", value)
    if match:
        return f"{match.group(1)}T{match.group(2)}Z"
    return iso_date(value)


# ---- HTML -> text ------------------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table",
               "section", "article", "header", "footer", "blockquote", "pre", "hr", "dd", "dt", "dl"}
_SKIP_TAGS = {"script", "style", "noscript", "template", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in ("li", "br"):
            return
        elif tag in ("ul", "ol"):
            self.parts.append("\n\n")      # a list ends its paragraph
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(value):
    """An HTML fragment -> readable plain text: block elements become line
    breaks, list items get a dash, runs of blank lines collapse."""
    value = value if isinstance(value, str) else None
    if not value:
        return None
    if "<" not in value:
        return multiline(value)
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return multiline(re.sub(r"<[^>]+>", " ", value))
    raw = "".join(parser.parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"(^|\n)- *\n+", r"\1- ", out)          # <li><p>…</p></li>: dash stays on its text
    out = re.sub(r"\n\n(- )", r"\n\1", out).strip()   # list items stay tight
    return out or None
