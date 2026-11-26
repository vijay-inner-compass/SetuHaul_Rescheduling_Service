# Two stages so that no Python source file reaches the final image (the server gets compiled bytecode only).
FROM python:3.12-slim AS compile
WORKDIR /build
COPY src ./src
COPY deploy/start.py ./deploy/
COPY scripts/set_admin_password.py ./scripts/
# -b writes each .pyc beside its source, where Python can import it once the source is gone.
RUN python -m compileall -q -b --invalidation-mode unchecked-hash src deploy scripts \
 && find . -name '*.py' -delete \
 && find . -name '__pycache__' -prune -exec rm -rf {} +

FROM python:3.12-slim
WORKDIR /app
# Dependencies only, from pyproject.toml; the application itself is found through PYTHONPATH.
COPY pyproject.toml docs/requirements-lock.txt /tmp/deps/
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/deps/pyproject.toml','rb'))['project']['dependencies']))" > /tmp/deps/requirements.txt \
 && pip install --no-cache-dir -r /tmp/deps/requirements.txt -c /tmp/deps/requirements-lock.txt \
 && rm -rf /tmp/deps
COPY --from=compile /build/ ./
COPY migrations ./migrations
COPY data/setuhaul.sqlite3 ./data/
RUN useradd --system app && mkdir state && chown app state
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONUTF8=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4)"]
CMD ["sh", "-c", "python deploy/start.pyc && exec uvicorn --factory setuhaul.api:create_app_from_env --host 0.0.0.0 --port 8000"]

# ForgeCI refuses an image without the revision label; /version reports GIT_COMMIT.
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision=$GIT_COMMIT
ENV GIT_COMMIT=$GIT_COMMIT
