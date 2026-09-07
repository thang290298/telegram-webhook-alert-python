import hmac
import os
import sys

from flask_httpauth import HTTPBasicAuth

auth = HTTPBasicAuth()

_username = os.environ.get('BASIC_AUTH_USERNAME')
_password = os.environ.get('BASIC_AUTH_PASSWORD')

# Endpoint /alert nhan webhook tu ben ngoai. Truoc day thieu env la tu dong
# chay bang admin/admin@123 - cap nay con duoc in ca trong README. Gio la
# fail-fast; ai thuc su muon giu mac dinh phai bat ALLOW_INSECURE_AUTH=true.
_allow_insecure = os.environ.get('ALLOW_INSECURE_AUTH', '').strip().lower() in ('1', 'true', 'yes')

if not _username or not _password:
    if not _allow_insecure:
        print(
            "[FATAL] auth: BASIC_AUTH_USERNAME / BASIC_AUTH_PASSWORD chua duoc set. "
            "Dat hai bien nay, hoac dat ALLOW_INSECURE_AUTH=true de chap nhan "
            "credentials mac dinh admin/admin@123 (KHONG dung cho production).",
            file=sys.stderr, flush=True
        )
        raise SystemExit(1)

    print(
        "[WARNING] auth: dang dung credentials mac dinh admin/admin@123 vi "
        "ALLOW_INSECURE_AUTH=true - KHONG an toan cho production!",
        file=sys.stderr, flush=True
    )
    _username = _username or 'admin'
    _password = _password or 'admin@123'

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
