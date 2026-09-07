from flask import Flask

# App duy nhat cua tien trinh. Truoc day file nay tao mot Flask app rieng con
# flaskAlert.py tao them mot cai nua -> `gunicorn app:app` chay ra app rong,
# khong co route nao, 404 toan bo. Gio chi con mot app; ca `app:app` lan
# `app.flaskAlert:app` deu tro ve dung doi tuong nay.
app = Flask(__name__)

from app import flaskAlert  # noqa: E402,F401  (dang ky route len `app`)
