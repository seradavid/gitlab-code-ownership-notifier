# Engine image for the ownership-notify CI/CD component.
#
# The image only needs the CLI: the two jobs talk to the GitLab API and to the
# Power Automate flow over HTTP, so Git and a checkout are not required
# (`GIT_STRATEGY: none` in the component).

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Packaging metadata first: this layer changes only when the packaging or the licence
# does, so `pip install` is not re-run for every code edit. The exec (JSON) form is used
# because it is unambiguous where the shell form depends on whitespace, and it keeps
# container linters happy about the directory copy.
COPY ["pyproject.toml", "/app/"]
COPY ["README.md", "/app/"]
COPY ["LICENSE", "/app/"]
COPY ["ownership_bot", "/app/ownership_bot/"]
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 bot

# The jobs run as a non-root user; there is nothing to write except the
# decision.json artifact in the working directory.
USER bot

ENTRYPOINT ["ownership-bot"]
CMD ["--help"]
