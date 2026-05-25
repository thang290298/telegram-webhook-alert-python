FROM python:3.9-slim

WORKDIR /prometheus-telegram-alert

RUN apt update && apt install -y \
    vim \
    bash \
    gcc \
    python3-dev \
    libffi-dev \
    libssl-dev \
    unzip \
    curl \
 && apt clean \
 && rm -rf /var/lib/apt/lists/* \
 && addgroup --system appgroup \
 && adduser --system --ingroup appgroup appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R appuser:appgroup /prometheus-telegram-alert

USER appuser

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.flaskAlert:app"]
