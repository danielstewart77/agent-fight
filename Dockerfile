FROM python:3.11-slim

WORKDIR /app

# ---- Docker CLI (client only) ----
# The app shells out to `docker` to run one ephemeral container per PR review
# (see app/container.py). It talks to the host daemon via the socket mounted in
# docker-compose.yml (Docker-out-of-Docker), so review containers run as
# siblings on the host, not nested. Debian's `docker.io` on slim ships the
# daemon but NOT the client binary, so copy the static client from the official
# image instead (version-pinned, no apt repo). `git` still comes from apt.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Application ----
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# SQLite lives here. Mount a volume over it to keep the database across
# rebuilds; the path is overridable with DB_PATH.
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/agent_fight.db

ENV PYTHONUNBUFFERED=1
EXPOSE 8890

CMD ["python", "-m", "app.entrypoint"]
