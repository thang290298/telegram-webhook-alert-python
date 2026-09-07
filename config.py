"""Doc va chuan hoa telegram_config.json thanh danh sach rule routing.

Ho tro 2 dang schema:

1. Dang MOI (khuyen nghi) - match dung theo label key, khong nhap nhang:

   {
     "PRIORITY_LABELS": ["site", "product"],
     "rules": {
       "storage-hpg": {
         "match": {"site": "hpg", "product": "storage"},
         "BOT_TOKEN": "...", "CHAT_ID": "...", "MESSAGE_THREAD_ID": "5"
       }
     }
   }

2. Dang CU (van chay duoc, khong can sua file dang deploy):

   {
     "PRIORITY_LABELS": ["site", "product", "service"],
     "service":       {"BOT_TOKEN": "...", "CHAT_ID": "..."},
     "site-provider": {"BOT_TOKEN": "...", "CHAT_ID": "..."}
   }

   Khac ban cu o mot diem quan trong: rule dang cu chi doi chieu voi value
   cua nhung label NAM TRONG PRIORITY_LABELS, thay vi toan bo labels.values().
   Truoc day mot alert co job="service" cung khop rule "service" -> gui nham
   chat. Key co dau '-' van thu match nguyen khoi truoc khi tach, nen mot
   label value nhu "site-provider" van route dung.

Do uu tien: nhieu dieu kien hon thang truoc; cung so dieu kien thi rule dang
moi ('match') thang rule dang cu; cuoi cung sap theo ten de deterministic.
"""

import json
import os
import sys
from typing import NamedTuple, Optional, Tuple

# Doc theo duong dan tuyet doi -> khong phu thuoc CWD cua tien trinh gunicorn.
# Override duoc bang env khi mount config ra cho khac.
CONFIG_PATH = os.environ.get(
    'TELEGRAM_CONFIG_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'telegram_config.json')
)


def _log(level, msg):
    print(f"[{level}] config: {msg}", file=sys.stderr, flush=True)


class Rule(NamedTuple):
    """Mot rule routing da chuan hoa.

    matchers: tuple cua (label_key, value).
              label_key = None nghia la "bat ky label nao trong PRIORITY_LABELS
              co value nay" (ngu nghia cua schema cu).
    alt_value: chi dung cho schema cu - cho phep key co dau '-' match nguyen
               khoi truoc khi bi tach thanh nhieu phan.
    """
    name: str
    matchers: Tuple[Tuple[Optional[str], str], ...]
    explicit: bool
    bot_token: str
    chat_id: str
    thread_id: Optional[str]
    alt_value: Optional[str] = None


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
    except OSError as e:
        _log('FATAL', f"khong doc duoc {path}: {e}")
        raise

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


def _norm_thread_id(value, rule_name):
    """Chuan hoa MESSAGE_THREAD_ID ve str hop le hoac None.

    Chuoi rong / khong phai so -> None (fallback ve DEFAULT_MESSAGE_THREAD_ID),
    thay vi de den luc gui moi phat hien ValueError.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        int(text)
    except ValueError:
        _log('ERROR', f"rule '{rule_name}': MESSAGE_THREAD_ID='{value}' khong phai so - bo qua thread")
        return None
    return text


def _build_rule(name, spec):
    """Chuyen mot entry trong config thanh Rule, hoac None neu khong hop le."""
    if not isinstance(spec, dict):
        _log('ERROR', f"rule '{name}' phai la object - bo qua")
        return None

    bot_token = spec.get('BOT_TOKEN')
    chat_id = spec.get('CHAT_ID')
    if not bot_token or not chat_id:
        _log('ERROR', f"rule '{name}' thieu BOT_TOKEN hoac CHAT_ID - bo qua, "
                      "alert khop key nay se ve DEFAULT bot")
        return None

    thread_id = _norm_thread_id(spec.get('MESSAGE_THREAD_ID'), name)
    raw_match = spec.get('match')

    # --- schema moi: match theo dung label key ---
    if raw_match is not None:
        if not isinstance(raw_match, dict) or not raw_match:
            _log('ERROR', f"rule '{name}': 'match' phai la object khong rong - bo qua")
            return None
        matchers = []
        for label_key, label_value in raw_match.items():
            if isinstance(label_value, (dict, list)):
                _log('ERROR', f"rule '{name}': match['{label_key}'] phai la gia tri don - bo qua rule")
                return None
            matchers.append((str(label_key), str(label_value)))
        return Rule(
            name=name,
            matchers=tuple(sorted(matchers)),
            explicit=True,
            bot_token=str(bot_token),
            chat_id=str(chat_id),
            thread_id=thread_id,
        )

    # --- schema cu: ten key chinh la value cua label, noi bang '-' ---
    parts = [p for p in name.split('-') if p]
    if not parts:
        _log('ERROR', f"rule '{name}': ten rule khong hop le - bo qua")
        return None

    return Rule(
        name=name,
        matchers=tuple((None, p) for p in parts),
        explicit=False,
        bot_token=str(bot_token),
        chat_id=str(chat_id),
        thread_id=thread_id,
        alt_value=name if len(parts) > 1 else None,
    )


def _build_rules(data):
    raw_rules = {}

    nested = data.get('rules')
    if nested is not None:
        if isinstance(nested, dict):
            raw_rules.update(nested)
        else:
            _log('ERROR', "'rules' phai la object - bo qua")

    for key, value in data.items():
        if key in ('PRIORITY_LABELS', 'rules'):
            continue
        if key in raw_rules:
            _log('WARNING', f"rule '{key}' co ca trong 'rules' lan o top-level - dung ban trong 'rules'")
            continue
        raw_rules[key] = value

    rules = []
    for name, spec in raw_rules.items():
        rule = _build_rule(name, spec)
        if rule is not None:
            rules.append(rule)

    # Nhieu dieu kien -> uu tien cao hon. Cung so dieu kien: schema moi thang
    # schema cu. Cuoi cung sap theo ten de thu tu deterministic giua cac lan boot.
    rules.sort(key=lambda r: (-len(r.matchers), 0 if r.explicit else 1, r.name))
    return rules


CONFIG_DATA = _load_config(CONFIG_PATH)

_raw_priority = CONFIG_DATA.get('PRIORITY_LABELS', [])
if not isinstance(_raw_priority, list):
    _log('ERROR', "PRIORITY_LABELS phai la mang - bo qua")
    _raw_priority = []
PRIORITY_LABELS = [str(x) for x in _raw_priority]

RULES = _build_rules(CONFIG_DATA)

if not RULES:
    _log('INFO', "khong co rule nao - tat ca alert dung DEFAULT_BOT_TOKEN / DEFAULT_CHAT_ID")
else:
    _log('INFO', f"nap {len(RULES)} rule routing: {', '.join(r.name for r in RULES)}")

if not PRIORITY_LABELS and any(not r.explicit for r in RULES):
    _log('WARNING', "co rule dang cu nhung PRIORITY_LABELS rong - se doi chieu voi "
                    "TAT CA label value, de route nham. Nen khai bao PRIORITY_LABELS "
                    "hoac chuyen sang dang 'match'")

DEFAULT_BOT_TOKEN = os.environ.get('DEFAULT_BOT_TOKEN')
DEFAULT_CHAT_ID = os.environ.get('DEFAULT_CHAT_ID')
DEFAULT_MESSAGE_THREAD_ID = _norm_thread_id(
    os.environ.get('DEFAULT_MESSAGE_THREAD_ID'), 'DEFAULT'
)

if not DEFAULT_BOT_TOKEN or not DEFAULT_CHAT_ID:
    _log('WARNING', "DEFAULT_BOT_TOKEN hoac DEFAULT_CHAT_ID chua duoc set - "
                    "moi alert khong khop rule se gui that bai")


def _rule_matches(rule, labels, priority_values):
    if rule.alt_value is not None and rule.alt_value in priority_values:
        return True
    for label_key, value in rule.matchers:
        if label_key is None:
            if value not in priority_values:
                return False
        elif str(labels.get(label_key, '')) != value:
            return False
    return True


def find_rule(labels):
    """Tra ve Rule khop dau tien theo do uu tien, hoac None neu khong khop."""
    if not RULES:
        return None
    if not isinstance(labels, dict):
        return None

    if PRIORITY_LABELS:
        priority_values = {str(labels[k]) for k in PRIORITY_LABELS if k in labels}
    else:
        # Khong khai bao PRIORITY_LABELS -> giu hanh vi cu (doi chieu moi label value)
        priority_values = {str(v) for v in labels.values()}

    for rule in RULES:
        if _rule_matches(rule, labels, priority_values):
            return rule
    return None
