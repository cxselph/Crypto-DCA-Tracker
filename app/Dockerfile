# Base image pinned by digest (python:3.14-slim, the same as Family Loans); bump it through a reviewed PR.
# The app uses only Python's standard library, so nothing is installed with pip.
FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# Debian security updates (signed repos) on top of the pinned base; tzdata so TZ sets the nightly backup time.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY tracker ./tracker
COPY static ./static

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["python", "-m", "tracker"]
