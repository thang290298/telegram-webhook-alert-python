from flask_httpauth import HTTPBasicAuth
import os
import sys

auth = HTTPBasicAuth()

_username = os.environ.get('BASIC_AUTH_USERNAME')
_password = os.environ.get('BASIC_AUTH_PASSWORD')

if not _username or not _password:
    print(
        "[WARNING] BASIC_AUTH_USERNAME or BASIC_AUTH_PASSWORD is not set. "
        "Using insecure default credentials - NOT safe for production!",
        file=sys.stderr
    )
    _username = _username or 'admin'
    _password = _password or 'admin@123'

users = {_username: _password}


@auth.verify_password
def verify_password(username, password):
    if username in users and users[username] == password:
        return username
