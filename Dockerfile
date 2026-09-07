# ---------- build stage: compile wheels, khong de lai toolchain trong image cuoi ----------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        libffi-dev \
        libssl-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- runtime stage ----------
FROM python:3.12-slim

# PYTHONUNBUFFERED: khong buffer stdout/stderr -> log ra ngay, khong mat log
# khi container crash luc boot (vd config JSON sai cu phap).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /prometheus-telegram-alert

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl \
        vim \
        bash \
        unzip \
        curl \
        netcat-openbsd \
 && (apt-get install -y --no-install-recommends telnet \
     || apt-get install -y --no-install-recommends inetutils-telnet) \
 && rm -rf /var/lib/apt/lists/* \
 && addgroup --system appgroup \
 && adduser --system --ingroup appgroup appuser

COPY --from=builder /install /usr/local
COPY --chown=appuser:appgroup . .

USER appuser

EXPOSE 9119

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9119/health || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.flaskAlert:app"]
