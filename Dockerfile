# The Social Manager, packaged for the cloud half of the hybrid deployment.
#
# This image holds the social manager and nothing else: the trading side of the
# repository (``selftrade/``, ``user_data/``, ``data/``) is not copied in, so a
# container physically cannot place an order.  The two libraries the package
# actually imports are installed by hand rather than from ``requirements.txt``,
# which exists for the trading runtime and brings numpy and pandas (about 200 MB)
# that no module under ``socialmanager/`` imports.
#
# Both commands below go through the same safety layer as the local CLI - the kill
# switch, the policy engine, the approval queue, the duplicate check and the
# connector readiness checks live inside the orchestrator and the publisher, not in
# the caller - so a cron job, a compose service and a hand-typed command are
# equally unable to publish something the policy engine refused:
#
#   python -m socialmanager tick      one worker tick (simulates unless ``--live``)
#   python -m socialmanager serve     the admin dashboard (a password is required to bind)
#
# Build and run from the repository root:
#   docker build -t selftrade-social .
#   cp .env.example .env    # then fill in the credentials you have
#   docker compose up -d
#
# Operator guide: docs/SOCIAL-MANAGER.md.  Variable names: .env.example.
FROM python:3.13-slim

# Unbuffered output so a structured log line reaches ``docker logs`` when it is
# written rather than when a 4 KB buffer happens to fill; no ``.pyc`` files, because
# the container's filesystem is disposable and a stale cache is only ever a source
# of confusion when a log line is compared against the source.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SOCIAL_CONFIG=/app/social.yaml

WORKDIR /app

# ``requests`` is imported lazily by the HTTP layer (socialmanager/http.py), the
# notifier and the OpenAI-compatible LLM provider; ``PyYAML`` is the configuration
# format.  The ranges match requirements.txt so the image and a local virtualenv
# resolve to the same major versions.
RUN pip install --no-cache-dir "requests>=2.31,<3" "PyYAML>=6,<7"

# A non-root user.  The uid is fixed at 10001 so a bind-mounted state directory on
# a Linux host can be handed over with ``chown -R 10001:10001 state``.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin social

COPY --chown=social:social socialmanager/ ./socialmanager/

# ``state/`` is the entire mutable surface: the SQLite database, the audit log, the
# generated media and the dashboard's password hash.  Everything else in the image
# is read-only, which is what makes the kill switch and the audit chain easy to
# reason about - nothing outside these files can change between two ticks.
RUN mkdir -p /app/state && chown -R social:social /app/state
VOLUME ["/app/state"]

USER social

# Liveness, not readiness: it opens the database, reads the kill switch and exits 0.
# ``python -m socialmanager doctor`` is the readiness check and it exits 1 while a
# connector is still unconfigured - the normal state of a draft-mode deployment -
# so it would mark this container unhealthy for being correctly configured.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
    CMD ["python", "-m", "socialmanager", "status", "--json"]

# Simulate by default.  The worker service in docker-compose.yml calls ``tick`` in a
# loop and adds ``--live`` only when SOCIAL_TICK_LIVE=1; a one-off real run is
# ``docker compose run --rm worker python -m socialmanager tick --live``.
CMD ["python", "-m", "socialmanager", "tick", "--limit", "5"]
