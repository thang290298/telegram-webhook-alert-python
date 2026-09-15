import hmac
import os
import sys

from flask_httpauth import HTTPBasicAuth

auth = HTTPBasicAuth()

# Credentials mac dinh khi khong set env. Endpoint /alert nhan webhook tu ben
# ngoai nen cap nay CHI hop le trong lab / noi bo; moi he thong dat that phai
# set BASIC_AUTH_USERNAME va BASIC_AUTH_PASSWORD.
DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = 'admin@123'

_username = os.environ.get('BASIC_AUTH_USERNAME') or ''
_password = os.environ.get('BASIC_AUTH_PASSWORD') or ''

# Hai bien deu khong bat buoc: thieu bien nao thi bien do lay mac dinh. Van in
# canh bao ra stderr de khong ai vo tinh chay production bang admin/admin@123
# ma khong biet.
_missing = []
if not _username:
    _username = DEFAULT_USERNAME
    _missing.append('BASIC_AUTH_USERNAME')
if not _password:
    _password = DEFAULT_PASSWORD
    _missing.append('BASIC_AUTH_PASSWORD')

if _missing:
    print(
        "[WARNING] auth: chua set " + " / ".join(_missing) +
        f" - dang dung mac dinh {DEFAULT_USERNAME}/{DEFAULT_PASSWORD}. "
        "KHONG an toan cho production, hay set hai bien nay.",
        file=sys.stderr, flush=True
    )

_username_b = _username.encode('utf-8')
_password_b = _password.encode('utf-8')


@auth.verify_password
def verify_password(username, password):
    """So sanh hang thoi gian (hmac.compare_digest) de khong ro ri do dai /
    tung ky tu cua credentials qua thoi gian phan hoi."""
    if not username or not password:
        return None

    user_ok = hmac.compare_digest(username.encode('utf-8'), _username_b)
    pass_ok = hmac.compare_digest(password.encode('utf-8'), _password_b)

    # Danh gia ca hai roi moi AND -> khong short-circuit theo username.
    if user_ok and pass_ok:
        return username
    return None
