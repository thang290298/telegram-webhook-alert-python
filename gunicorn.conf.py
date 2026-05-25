bind = "0.0.0.0:9119"

# Keep workers=1 so the in-memory TTLCache dedup works correctly across requests.
# Multiple workers each have their own cache -> duplicate alerts would still be sent.
# To scale horizontally, replace TTLCache with a shared Redis cache instead.
workers = 1
threads = 8
timeout = 120
wsgi_app = "app.flaskAlert:app"
