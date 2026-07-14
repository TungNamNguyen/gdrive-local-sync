# Seagate <-> Google Drive sync — hardened image.
# Runs as a non-root user (UID 1000); the container is meant to run with a
# read-only rootfs + tmpfs /tmp (see docker-compose.yml), so nothing outside
# the mounted secrets/ and data/ volumes is written at runtime.
FROM python:3.12-slim

# - No .pyc writes (read-only rootfs friendly), unbuffered logs.
# - HOME=/tmp so Streamlit's config/cache land on the tmpfs, not the rootfs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code (services/, UI, scripts, tests) + Streamlit config.
COPY .streamlit/ ./.streamlit/
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

# Non-root user (UID 1000) owns the writable data/secrets mount points.
RUN useradd --uid 1000 --create-home --home-dir /home/appuser appuser \
    && mkdir -p /app/secrets /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

# Streamlit serves on 0.0.0.0 inside the container; the host only exposes it on
# 127.0.0.1 (docker-compose.yml). headless=true skips the "open browser" prompt.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=3).status==200 else 1)"

CMD ["streamlit", "run", "app/main.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
