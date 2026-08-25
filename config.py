import os
import json
import sys

# Doc theo duong dan tuyet doi -> khong phu thuoc CWD cua tien trinh gunicorn.
# Override duoc bang env khi mount config ra cho khac.
CONFIG_PATH = os.environ.get(
    'TELEGRAM_CONFIG_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'telegram_config.json')
)


def _log(level, msg):
    print(f"[{level}] config: {msg}", file=sys.stderr)


def _load_config(path):
    """Doc telegram_config.json mot cach chiu loi.

    - File thieu / rong : -> {} . Day la cau hinh hop le "khong dung mapping",
                             tat ca alert ve DEFAULT bot. Khong lam chet worker.
    - File co BOM       : utf-8-sig tu bo 3 byte EF BB BF (file soan tren Windows).
    - JSON sai cu phap  : FATAL, dung han. Day la loi go nham chu khong phai y do;
                          neu am tham fallback ve {} thi alert se chay nham sang
                          chat DEFAULT ma khong ai biet.
    """
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            raw = f.read()
    except FileNotFoundError:
        _log('WARNING', f"khong tim thay {path} - chay khong mapping, tat ca alert ve DEFAULT bot")
        return {}

    if not raw.strip():
        _log('WARNING', f"{path} rong - chay khong mapping, tat ca alert ve DEFAULT bot")
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _log('FATAL', f"{path} sai cu phap JSON: {e}")
        raise

    if not isinstance(data, dict):
        _log('FATAL', f"{path} phai la JSON object {{...}}, dang la {type(data).__name__}")
        raise ValueError(f"{path}: expected a JSON object")

    return data


CONFIG_DATA = _load_config(CONFIG_PATH)

PRIORITY_LABELS = CONFIG_DATA.get('PRIORITY_LABELS', [])

# Loai bo entry thieu BOT_TOKEN/CHAT_ID ngay tu luc boot.
# Neu de lot, flaskAlert.py se KeyError giua luc xu ly webhook -> tra 500,
# lam mat nhung alert da duoc reserve trong batch do.
TELEGRAM_CONFIG = {}
for _key, _value in CONFIG_DATA.items():
    if _key == 'PRIORITY_LABELS':
        continue
    if not isinstance(_value, dict) or not _value.get('BOT_TOKEN') or not _value.get('CHAT_ID'):
        _log('ERROR', f"mapping '{_key}' thieu BOT_TOKEN hoac CHAT_ID - bo qua, alert khop key nay se ve DEFAULT bot")
        continue
    TELEGRAM_CONFIG[_key] = _value

if not TELEGRAM_CONFIG:
    _log('INFO', "khong co mapping nao - tat ca alert dung DEFAULT_BOT_TOKEN / DEFAULT_CHAT_ID")

DEFAULT_BOT_TOKEN = os.environ.get('DEFAULT_BOT_TOKEN')
DEFAULT_CHAT_ID = os.environ.get('DEFAULT_CHAT_ID')
DEFAULT_MESSAGE_THREAD_ID = os.environ.get('DEFAULT_MESSAGE_THREAD_ID')

if not DEFAULT_BOT_TOKEN or not DEFAULT_CHAT_ID:
    _log('WARNING', "DEFAULT_BOT_TOKEN hoac DEFAULT_CHAT_ID chua duoc set - "
                    "moi alert khong khop mapping se gui that bai")
