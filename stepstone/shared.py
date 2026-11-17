"""Value helpers for the StepStone parsers: safe nested access, numbers,
text, ISO dates and HTML -> text. Pure functions, no network; every helper
tolerates None and junk.

Same semantics as jobstreet/shared.py and seek/shared.py (g / as_list / as_dict,
num, to_int, rounded, text, multiline, iso_date, iso_datetime, html_to_text); the
ones every site shares come from scraper_utils and the rest live here (the
stepstone package ships as its own open-source repo, which must not pull in other
sites' modules). One difference: html_to_text keeps a list item's dash on its
text when the item wraps a paragraph (<li><p>…</p></li>, common on StepStone)."""
import re
from datetime import datetime
from html.parser import HTMLParser
from scraper_utils.text import clean as text, multiline
from scraper_utils.dates import iso_date, iso_datetime
from scraper_utils.values import as_dict, as_list, g, num, rounded, to_int_round as to_int

# ---- text / dates --------------------------------------------------------------------------


_LONG_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y")


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
