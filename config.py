"""Configuration for the Stepstone Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT             port the API listens on (default 8000)
    STEPSTONE_PROXY  proxy URL for every request, e.g. http://user:pass@host:port
                     (default: none — direct). Every Stepstone Group board sits
                     behind Akamai, which refuses datacenter IPs and many office
                     networks ("blocked the request" errors). A residential
                     proxy gets through on every board. If your provider can
                     target countries, put `{country}` in the URL and each call
                     uses the board's own country (de, at, be, nl, gb, za):
                     http://user-country-{country}:pass@host:port

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

# TLS identity curl_cffi impersonates for every session.
STEPSTONE_IMPERSONATIONS = ("chrome",)
STEPSTONE_REQUESTS_PER_SESSION = 100     # retire a session (one proxy exit) after this many calls
STEPSTONE_MAX_ATTEMPTS = MAX_RETRIES     # fresh sessions tried per call before giving up
STEPSTONE_WORKERS = 4                    # parallel upstream calls inside one request (jobs/batch)

# This kit runs browser-free: plain HTTP with browser-grade TLS. The
# headless-browser fallback stays off.
STEPSTONE_BROWSER_FALLBACK = False
STEPSTONE_BROWSER_IDLE_SECONDS = 300

STEPSTONE_PROXY = os.environ.get("STEPSTONE_PROXY") or None


def stepstone_proxy(country=None):
    """The proxy a new session uses (None = direct). A `{country}` in
    STEPSTONE_PROXY is replaced with the board's country code."""
    proxy = os.environ.get("STEPSTONE_PROXY") or None
    if proxy and country:
        proxy = proxy.replace("{country}", country)
    return proxy
