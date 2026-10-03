FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN addgroup --system reciapp && adduser --system --ingroup reciapp reciapp \
    && chown -R reciapp:reciapp /app

USER reciapp

ENV PYTHONUNBUFFERED=1
ENV TRUSTED_PROXY_IPS=127.0.0.1

HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready')"

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="${TRUSTED_PROXY_IPS}" --no-server-header
