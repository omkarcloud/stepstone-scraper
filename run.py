"""Start the Stepstone Scraper API.

    python run.py            # http://localhost:8000
    PORT=9000 python run.py  # another port

Then:  curl "http://localhost:8000/jobs/details?job=14448261"
"""
import bottle
from cheroot import wsgi

import config
import routes  # noqa: F401  (mounts the routes on bottle's default app)


def main():
    app = bottle.default_app()
    print(f"Stepstone Scraper listening on http://localhost:{config.PORT}/")
    print(f"Try:  curl \"http://localhost:{config.PORT}/jobs/details?job=14448261\"")
    if not config.STEPSTONE_PROXY:
        print("Tip:  Stepstone's Akamai refuses datacenter IPs — set STEPSTONE_PROXY to a residential proxy "
              "if calls come back as \"blocked the request\".")
    server = wsgi.Server(("0.0.0.0", config.PORT), app, server_name="stepstone-scraper", numthreads=16)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
