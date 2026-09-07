import os

bind = os.environ.get('BIND', '0.0.0.0:9119')

# Keep workers=1 so the in-memory TTLCache dedup works correctly across requests.
# Multiple workers each have their own cache -> duplicate alerts would still be sent,
# and each worker would start its own background sender thread.
# To scale horizontally, replace TTLCache with a shared Redis cache instead
# (see the note at the bottom of app/flaskAlert.py).
workers = 1
threads = int(os.environ.get('GUNICORN_THREADS', 8))
timeout = int(os.environ.get('GUNICORN_TIMEOUT', 120))
graceful_timeout = 30
keepalive = 5

wsgi_app = "app.flaskAlert:app"

accesslog = None if os.environ.get('ACCESS_LOG', '').lower() in ('', '0', 'false', 'no') else '-'
errorlog = '-'
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info')
