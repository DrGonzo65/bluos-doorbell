FROM python:3.12-slim

LABEL org.opencontainers.image.title="BluOS Doorbell" \
      org.opencontainers.image.description="Doorbell chime for Bluesound players with correct volume and source restore"

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY tools/ ./tools/

ENV DOORBELL_CONFIG=/config/config.yaml \
    DOORBELL_CHIME_DIR=/chimes \
    PYTHONUNBUFFERED=1

# The listen port comes from config.yaml, not from here.
EXPOSE 8095

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request,yaml; \
p=yaml.safe_load(open(os.environ['DOORBELL_CONFIG'])).get('listen_port',8095); \
urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4)" || exit 1

CMD ["python", "-m", "app"]
