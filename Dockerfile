# syntax=docker/dockerfile:1
# ---- frontend build ---------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npx tsc -b && npx vite build --outDir /out --emptyOutDir

# ---- python deps ------------------------------------------------------------------
FROM python:3.13-alpine AS deps
RUN apk add --no-cache build-base libffi-dev
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# ---- runtime ----------------------------------------------------------------------
FROM python:3.13-alpine
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="iSponsorBlockTV Web UI" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Secure web UI for iSponsorBlockTV: pairing, configuration, statistics and diagnostics" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/irnutsmurt/isponsorblocktv-webui"
RUN apk add --no-cache tzdata && \
    addgroup -g 1000 webui && adduser -D -H -u 1000 -G webui webui && \
    mkdir -p /data /upstream-data && chown 1000:1000 /data
COPY --from=deps /install /usr/local
WORKDIR /app
COPY backend/webui ./webui
COPY --from=web /out ./webui/static
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data UPSTREAM_DATA_DIR=/upstream-data PORT=8099
USER 1000:1000
EXPOSE 8099
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,ssl;s='https' if (os.getenv('TLS_SELF_SIGNED','').lower() in ('1','true','yes') or os.getenv('TLS_CERT_FILE')) else 'http';urllib.request.urlopen(f'{s}://127.0.0.1:'+os.getenv('PORT','8099')+'/healthz',timeout=4,context=ssl._create_unverified_context())" || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "webui"]
