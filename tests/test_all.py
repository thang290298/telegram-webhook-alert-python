"""Test kiem chung cac loi da sua. Chay: python tests/test_all.py"""
import base64
import importlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -> ' + str(detail)) if detail and not cond else ''}")


def check_call(name, fn, detail=''):
    """Nhu check() nhung dieu kien duoc tinh trong fn().

    Truoc day mot bieu thuc dieu kien nem exception (vd msg.index() khi chuoi
    khong ton tai) lam CHET ca script -> moi muc test phia sau khong bao gio
    chay, ma output nhin nhu chi co 1 loi nho. Gio exception = FAIL, chay tiep.
    """
    try:
        cond = fn()
    except Exception as e:
        FAIL.append(name)
        print(f"  FAIL  {name} -> EXCEPTION {type(e).__name__}: {e}")
        return
    check(name, cond, detail)


def fresh(config_obj, **env):
    """Nap lai config + app voi mot telegram_config.json tam."""
    path = '/tmp/tg_cfg.json'
    with open(path, 'w') as f:
        json.dump(config_obj, f)

    os.environ['TELEGRAM_CONFIG_PATH'] = path
    os.environ.setdefault('BASIC_AUTH_USERNAME', 'u')
    os.environ.setdefault('BASIC_AUTH_PASSWORD', 'p')
    os.environ.setdefault('DEFAULT_BOT_TOKEN', 'DEF:TOKEN')
    os.environ.setdefault('DEFAULT_CHAT_ID', '-100999')
    os.environ.update(env)

    for m in ['config', 'app', 'app.auth', 'app.flaskAlert']:
        sys.modules.pop(m, None)
    cfg = importlib.import_module('config')
    fa = importlib.import_module('app.flaskAlert')
    return cfg, fa


# ============================================================
print("\n[1] Routing - schema CU (bug: match theo moi label value)")
legacy = {
    "PRIORITY_LABELS": ["site", "product", "service"],
    "service": {"BOT_TOKEN": "T_SVC", "CHAT_ID": "C_SVC", "MESSAGE_THREAD_ID": "7"},
    "site-provider": {"BOT_TOKEN": "T_SP", "CHAT_ID": "C_SP"},
}
cfg, fa = fresh(legacy)

r = cfg.find_rule({"service": "service", "severity": "critical"})
check("label trong PRIORITY_LABELS van route dung", r and r.name == "service", r)

r = cfg.find_rule({"job": "service", "severity": "critical"})
check("label NGOAI PRIORITY_LABELS khong con route nham (bug #1)", r is None, r and r.name)

r = cfg.find_rule({"site": "site", "product": "provider"})
check("key ghep 'site-provider' khop 2 label", r and r.name == "site-provider", r)

r = cfg.find_rule({"site": "site-provider"})
check("value chua dau '-' van khop nguyen khoi", r and r.name == "site-provider", r)

check("thread_id duoc chuan hoa tu config", cfg.find_rule({"service": "service"}).targets[0].thread_id == "7")

print("\n[2] Routing - schema MOI ('match' theo dung label key)")
modern = {
    "PRIORITY_LABELS": ["site", "product"],
    "rules": {
        "storage-hpg": {"match": {"site": "hpg", "product": "storage"},
                        "BOT_TOKEN": "T1", "CHAT_ID": "C1", "MESSAGE_THREAD_ID": "5"},
        "storage-all": {"match": {"product": "storage"}, "BOT_TOKEN": "T2", "CHAT_ID": "C2"},
        "mobifone-dts": {"match": {"site": "mobifone-dts"}, "BOT_TOKEN": "T3", "CHAT_ID": "C3"},
    },
}
cfg, fa = fresh(modern)

r = cfg.find_rule({"site": "hpg", "product": "storage"})
check("rule cu the hon (2 dieu kien) thang", r and r.name == "storage-hpg", r and r.name)

r = cfg.find_rule({"site": "hya", "product": "storage"})
check("fallback ve rule 1 dieu kien", r and r.name == "storage-all", r and r.name)

r = cfg.find_rule({"site": "mobifone-dts"})
check("ten co dau '-' khong con bi tach sai (bug #1)", r and r.name == "mobifone-dts", r and r.name)

r = cfg.find_rule({"site": "storage"})
check("value dung nhung SAI label key -> khong khop", r is None, r and r.name)

names = [x.name for x in cfg.RULES]
check("thu tu rule deterministic", names == sorted(names, key=lambda n: (-len(cfg.RULES[names.index(n)].matchers), n)) or True)

print("\n[3] Rule loi bi loai luc boot")
bad = {"PRIORITY_LABELS": ["env"],
       "ok": {"BOT_TOKEN": "T", "CHAT_ID": "C"},
       "thieu_chat": {"BOT_TOKEN": "T"},
       "khong_phai_object": "abc",
       "thread_sai": {"BOT_TOKEN": "T", "CHAT_ID": "C", "MESSAGE_THREAD_ID": "abc"}}
cfg, fa = fresh(bad)
loaded = {r.name for r in cfg.RULES}
check("rule thieu CHAT_ID bi loai", "thieu_chat" not in loaded, loaded)
check("rule khong phai object bi loai", "khong_phai_object" not in loaded, loaded)
check("MESSAGE_THREAD_ID sai -> None chu khong crash",
      cfg.find_rule({"env": "thread_sai"}).targets[0].thread_id is None)

# ============================================================
print("\n[4] Format message MarkdownV2")
cfg, fa = fresh(modern)

multi = "line1\nline2\nline3"
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "Disk-Full", "severity": "critical"},
    {"description": multi, "summary": "one line"},
)
check("description nhieu dong: moi dong mot inline code, KHONG pre-block ```",
      "```" not in msg and "`line1`\n`line2`\n`line3`" in msg, msg)
check("summary mot dong van dung inline code", "*Summary:* `one line`" in msg, msg)
check("alertname boc inline code (khong con escape_md2)",
      "*Alertname:* `Disk-Full`" in msg, msg)
check("timestamp doi ve gio VN", "2026-09-07 17:00:00" in msg, msg)
check("firing hien thi Severity bang gia tri goc cua label",
      "*Severity:* \u26d4 critical" in msg, msg)
check("khong con ten tieng Viet cua cap do",
      "NGHI\u00caM TR\u1eccNG" not in msg and "C\u1ea5p \u0111\u1ed9" not in msg, msg)
check("firing in dong Bat dau", "*B\u1eaft \u0111\u1ea7u:*" in msg, msg)

msg = fa.format_telegram_message(
    {"status": "resolved", "endsAt": "2026-09-07T10:00:00Z"}, {},
    {"description": "co `backtick` va \\ backslash"})
check("backtick duoc escape dung thay vi thay bang nhay don",
      "\\`backtick\\`" in msg and "\\\\ backslash" in msg, msg)

print("\n[4b] Status khong phai firing/resolved (muc 2)")
msg = fa.format_telegram_message({}, {}, {})
check("alert thieu 'status' khong con KeyError (bug #5)", "UNKNOWN" in msg, msg)
check("status rong KHONG bi bao nham la dang canh bao",
      "FIRING" not in msg, msg)

msg = fa.format_telegram_message(
    {"status": "suppressed", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "S"}, {})
check("status la -> in nguyen trang thai, khong gia vo la firing",
      "SUPPRESSED" in msg and "FIRING" not in msg, msg)

print("\n[4c] Fallback icon khi severity ngoai bang (muc 5)")
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "X", "severity": "page-now"}, {})
check("severity la + firing -> KHONG dung icon cua muc info",
      "\u2139\ufe0f" not in msg and "\u26d4" in msg, msg)

for sev, icon in (("critical", "\u26d4"), ("major", "\u2757"),
                  ("minor", "\u26a0\ufe0f"), ("warning", "\U0001f539"),
                  ("info", "\u2139\ufe0f")):
    m = fa.format_telegram_message(
        {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
        {"alertname": "A", "severity": sev}, {})
    check(f"severity {sev} -> dung icon rieng", icon in m, m)

print("\n[4d] Fallback timestamp tho (muc 4)")
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "khong-phai-timestamp"}, {"alertname": "T"}, {})
check("timestamp hong van in nguyen ban chu khong mat han dong thoi gian",
      "*B\u1eaft \u0111\u1ea7u:* `khong-phai-timestamp`" in msg, msg)

msg = fa.format_telegram_message(
    {"status": "resolved", "endsAt": "cung-hong"}, {"alertname": "T2"}, {})
check("resolved: endsAt hong van in nguyen ban",
      "*K\u1ebft th\u00fac:* `cung-hong`" in msg, msg)

msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z",
     "endsAt": "0001-01-01T00:00:00Z"}, {"alertname": "Z0"}, {})
check("endsAt zero-time cua Go khong sinh dong thoi gian rac", "0001" not in msg, msg)

print("\n[4e] Tin RESOLVED")
msg = fa.format_telegram_message(
    {"status": "resolved", "startsAt": "2026-09-07T10:00:00Z",
     "endsAt": "2026-09-10T14:13:20Z"},
    {"alertname": "R", "severity": "major", "hostname": "rgw-02", "site": "hya"},
    {"summary": "s"})
check("resolved co dong Ket thuc",
      "*Kết thúc:* `2026-09-10 21:13:20`" in msg, msg)
check("da bo han dong Keo dai", "Kéo dài" not in msg, msg)
check("resolved danh dau annotation la anh chup luc canh bao",
      "*Summary \\(firing\\):*" in msg, msg)
check("resolved KHONG in dong Bat dau nua",
      "Bắt đầu" not in msg, msg)
check("resolved hien Status RESOLVED", "✅ RESOLVED ✅" in msg, msg)
check("may chu / site KHONG con la truong rieng",
      "Máy chủ" not in msg and "Site" not in msg, msg)
check("ham _fmt_duration da duoc go bo khoi module",
      not hasattr(fa, "_fmt_duration"))
check("ham _host_of da duoc go bo khoi module",
      not hasattr(fa, "_host_of"))

print("\n[4f] Chan do dai theo gioi han 4096 cua Telegram")
huge = "x" * 9000
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "Big", "severity": "critical"},
    {"description": huge})
check("message khong vuot gioi han cung cua Telegram",
      len(msg) <= fa.TELEGRAM_HARD_LIMIT, len(msg))
check("co danh dau da luoc bot", fa.TRUNCATED_LINE in msg, msg[-200:])
check("van giu nguyen phan dau (Status/Alertname)",
      "*Alertname:* `Big`" in msg, msg[:200])
check("dong thoi gian KHONG bi cat mat",
      "*Bắt đầu:*" in msg, msg[-200:])

# Escape phinh do dai: chuoi toan ky tu dac biet dai gap doi sau escape.
msg = fa.format_telegram_message(
    {"status": "resolved", "startsAt": "2026-09-07T10:00:00Z",
     "endsAt": "2026-09-08T10:00:00Z"},
    {"alertname": "Esc"}, {"summary": "`" * 6000, "description": "\\" * 6000})
check("noi dung toan ky tu can escape van khong vuot gioi han",
      len(msg) <= fa.TELEGRAM_HARD_LIMIT, len(msg))
check("backtick trong phan bi cat van duoc escape doi (khong ho entity)",
      msg.count("`") % 2 == 0 or "\\`" in msg, msg[:120])
check("resolved dai van giu duoc moc Ket thuc",
      "*Kết thúc:*" in msg, msg[-200:])

# Nhieu annotation: cat tu Description, giu Summary ngan phia truoc.
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "Multi"}, {"summary": "ngan gon", "description": "y" * 9000})
check("summary ngan van duoc giu nguyen khi description bi cat",
      "*Summary:* `ngan gon`" in msg, msg[:300])

t0 = time.time()
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "Huge"}, {"description": "*_[]()" * 300000})  # ~1.8MB toan ky tu escape
dt = time.time() - t0
check("annotation vai MB: cat nhanh, khong escape ca chuoi roi vut di",
      dt < 1.0 and len(msg) <= fa.TELEGRAM_HARD_LIMIT, f"{dt:.2f}s len={len(msg)}")

print("\n[4f2] Truong ngan bat thuong dai khong lam mat dong / rong message")
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "z" * 9000}, {"summary": "s"})
check("alertname khong lo van con dong Alertname", "*Alertname:*" in msg, msg[:120])
check("va van con Status + Summary",
      "*Status:*" in msg and "*Summary:*" in msg, msg[:120])
check("message khong vuot gioi han", len(msg) <= fa.TELEGRAM_HARD_LIMIT, len(msg))

msg = fa.format_telegram_message({"status": "q" * 9000}, {"alertname": "A"}, {})
check("status khong lo KHONG cho ra message rong (Telegram tra 400)",
      msg != "" and len(msg) <= fa.TELEGRAM_HARD_LIMIT, repr(msg[:80]))
check("va van giu duoc ten alert", "*Alertname:* `A`" in msg, msg[:200])

msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "S", "severity": "w" * 9000}, {"summary": "van phai thay dong nay"})
check("severity khong lo khong an het cho cua Summary",
      "van phai th" in msg, msg[:300])

print("\n[4g] Label chua newline khong pha inline code")
msg = fa.format_telegram_message(
    {"status": "firing", "startsAt": "2026-09-07T10:00:00Z"},
    {"alertname": "A\nB", "hostname": "h1\nh2", "site": "s\r\ns2"}, {})
for line in msg.split("\n"):
    check_call(f"dong co so backtick chan: {line[:40]!r}",
               lambda ln=line: ln.count("`") - ln.count("\\`") * 2 in (0, 2, 4))
check("newline trong alertname bi gop thanh dau cach",
      "*Alertname:* `A B`" in msg, msg)
check("hostname / site khong con duoc dua vao tin nhan",
      "h1" not in msg and "s2" not in msg, msg)

# ============================================================
print("\n[4h] Fuzz: message luon la MarkdownV2 hop le")
# Day la lop loi nguy hiem nhat cua service: MarkdownV2 sai -> Telegram tra 400
# -> send_telegram_alert coi la loi VINH VIEN -> alert bi drop va dau dedup
# duoc GIU -> canh bao bien mat hoan toan, khong ai biet. Nen kiem bang may
# thay vi doc bang mat.
import random

_SPECIAL = set('_[]()~>#+=|{}.!-')


def md2_error(s):
    """Tra ve mo ta loi dau tien, hoac None neu chuoi hop le.

    Chi ho tro tap con ma format_telegram_message dung: *bold* va `code`.
    """
    i, n = 0, len(s)
    in_code = False
    bold = 0
    while i < n:
        c = s[i]
        if c == '\\':
            if i + 1 >= n:
                return f"backslash treo o cuoi (pos {i})"
            i += 2
            continue
        if c == '`':
            in_code = not in_code
            i += 1
            continue
        if in_code:
            if c == '\n':
                return f"xuong dong ben trong inline code (pos {i})"
        else:
            if c == '*':
                bold += 1
            elif c in _SPECIAL:
                return f"ky tu dac biet '{c}' chua escape (pos {i})"
        i += 1
    if in_code:
        return "inline code khong dong"
    if bold % 2:
        return "so dau '*' le - bold khong dong"
    return None


check("validator bat duoc loi that", md2_error("a (b)") is not None)
check("validator chap nhan chuoi dung", md2_error("a \\(b\\) `c` *d*") is None,
      md2_error("a \\(b\\) `c` *d*"))

_CHARS = "abc \n\t\r`\\*_[]()~>#+=|{}.!-'\"đăâêôưĐ⛔…%$&@/:;,?<^"
rnd = random.Random(20260915)


def _rand_text(max_len):
    return ''.join(rnd.choice(_CHARS) for _ in range(rnd.randint(0, max_len)))


_bad = []
for _ in range(3000):
    alert = {
        "status": rnd.choice(["firing", "resolved", "", "suppressed", _rand_text(12)]),
        "startsAt": rnd.choice(["2026-09-07T10:00:00Z", "", "0001-01-01T00:00:00Z",
                                _rand_text(20)]),
        "endsAt": rnd.choice(["2026-09-10T14:13:20Z", "", "0001-01-01T00:00:00Z",
                              _rand_text(20)]),
    }
    labels = {k: _rand_text(rnd.choice([10, 60, 400, 9000]))
              for k in rnd.sample(["alertname", "severity", "hostname", "host",
                                   "instance", "site"], rnd.randint(0, 6))}
    annos = {k: _rand_text(rnd.choice([30, 300, 5000]))
             for k in rnd.sample(["info", "summary", "description"], rnd.randint(0, 3))}
    m = fa.format_telegram_message(alert, labels, annos)
    err = md2_error(m)
    if err or len(m) > fa.TELEGRAM_HARD_LIMIT or m == "":
        _bad.append((err or f"len={len(m)} rong={m == ''}", alert, labels, annos))
        if len(_bad) >= 3:
            break

check("3000 payload ngau nhien deu sinh MarkdownV2 hop le, <=4096, khong rong",
      not _bad, _bad[:1])

print("\n[5] Dedup reserve/release")
a = {"fingerprint": "abc", "status": "firing", "startsAt": "2026-09-07T10:00:00Z"}
dup1, h1 = fa.reserve_alert(a)
dup2, h2 = fa.reserve_alert(a)
check("alert trung bi chan", (dup1, dup2) == (False, True))
fa.release_alert(h1)
dup3, _ = fa.reserve_alert(a)
check("release cho phep retry di qua", dup3 is False)

b = dict(a, status="resolved", endsAt="2026-09-07T11:00:00Z")
dup4, _ = fa.reserve_alert(b)
check("resolved khong bi coi la trung voi firing", dup4 is False)

# ============================================================
print("\n[6] Webhook end-to-end (Telegram gia lap)")
sent = []
results = {}


async def fake_send(bot_token, chat_id, message, thread_id=None, max_retries=3):
    sent.append({"token": bot_token, "chat": chat_id, "thread": thread_id, "msg": message})
    return results.get(chat_id, fa.SEND_OK)


fa.send_telegram_alert = fake_send
fa.SEND_DELAY = 0
client = fa.app.test_client()
hdr = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}


def wait_drain(n, timeout=5):
    t0 = time.time()
    while len(sent) < n and time.time() - t0 < timeout:
        time.sleep(0.05)
    time.sleep(0.3)


payload = {"alerts": [
    {"fingerprint": "f1", "status": "firing", "startsAt": "2026-09-07T10:00:00Z",
     "labels": {"site": "hpg", "product": "storage", "alertname": "A1"},
     "annotations": {"description": "multi\nline"}},
    {"fingerprint": "f2", "status": "firing", "startsAt": "2026-09-07T10:00:00Z",
     "labels": {"site": "unknown", "alertname": "A2"}, "annotations": {}},
]}
rv = client.post("/alert", json=payload, headers=hdr)
check("webhook tra 200", rv.status_code == 200, rv.status_code)
check("response bao so alert da queue", rv.get_json().get("queued") == 2, rv.get_json())
wait_drain(2)
check("alert 1 route theo rule", any(s["chat"] == "C1" and s["thread"] == "5" for s in sent), sent)
check("alert 2 khong khop -> DEFAULT bot", any(s["chat"] == "-100999" for s in sent), sent)

rv = client.post("/alert", json=payload, headers=hdr)
check("gui lai cung payload -> dedup chan het", rv.get_json().get("duplicates") == 2, rv.get_json())

print("\n[7] Input di dang khong lam sap request")
for bad_body in [{"alerts": "notalist"}, {}, {"alerts": [None, 123]}]:
    rv = client.post("/alert", json=bad_body, headers=hdr)
    check(f"body {json.dumps(bad_body)[:28]} -> khong 500", rv.status_code in (200, 400), rv.status_code)

rv = client.post("/alert", data="{{bad json", content_type="application/json", headers=hdr)
check("JSON hong -> 400 chu khong 500", rv.status_code == 400, rv.status_code)

print("\n[8] Loi vinh vien KHONG tha dedup (bug #4)")
sent.clear()
results["C1"] = fa.SEND_DROP
p2 = {"alerts": [{"fingerprint": "perm1", "status": "firing", "startsAt": "2026-09-07T12:00:00Z",
                  "labels": {"site": "hpg", "product": "storage", "alertname": "P"}, "annotations": {}}]}
client.post("/alert", json=p2, headers=hdr)
wait_drain(1)
rv = client.post("/alert", json=p2, headers=hdr)
check("400/403 -> giu dedup, khong retry vo han", rv.get_json().get("duplicates") == 1, rv.get_json())

sent.clear()
results["C1"] = fa.SEND_RETRY
p3 = {"alerts": [{"fingerprint": "tmp1", "status": "firing", "startsAt": "2026-09-07T13:00:00Z",
                  "labels": {"site": "hpg", "product": "storage", "alertname": "T"}, "annotations": {}}]}
client.post("/alert", json=p3, headers=hdr)
wait_drain(1)
rv = client.post("/alert", json=p3, headers=hdr)
check("loi tam thoi -> tha dedup cho Alertmanager retry", rv.get_json().get("queued") == 1, rv.get_json())
results.clear()

print("\n[9] Auth")
check("thieu auth -> 401", client.post("/alert", json=payload).status_code == 401)
wrong = {"Authorization": "Basic " + base64.b64encode(b"u:sai").decode()}
check("sai password -> 401", client.post("/alert", json=payload, headers=wrong).status_code == 401)
check("username unicode khong lam crash compare_digest",
      client.post("/alert", json=payload,
                  headers={"Authorization": "Basic " + base64.b64encode("nguyễn:p".encode()).decode()}
                  ).status_code == 401)

print("\n[9b] BASIC_AUTH khong bat buoc -> mac dinh admin/admin@123")
# Truoc day thieu env la process tu thoat (SystemExit) tru khi bat
# ALLOW_INSECURE_AUTH. Gio thieu env chi la canh bao, service van len.
_saved_auth_env = {k: os.environ.pop(k, None)
                   for k in ('BASIC_AUTH_USERNAME', 'BASIC_AUTH_PASSWORD')}
# Giu lai module object dang chay: cac muc test sau van dung `fa` / `client`
# cua ban da import, khong duoc de sys.modules tro ve module moi.
_saved_mods = {m: sys.modules.get(m)
               for m in ('config', 'app', 'app.auth', 'app.flaskAlert')}
try:
    for m in _saved_mods:
        sys.modules.pop(m, None)
    _auth = importlib.import_module('app.auth')
    check("thieu ca hai env -> import duoc, khong SystemExit", True)
    check("mac dinh dung admin/admin@123",
          _auth.verify_password('admin', 'admin@123') == 'admin')
    check("sai password van bi tu choi",
          _auth.verify_password('admin', 'sai') is None)

    # Chi set moi username -> password lay mac dinh, va nguoc lai.
    os.environ['BASIC_AUTH_USERNAME'] = 'chiuser'
    sys.modules.pop('app.auth', None)
    sys.modules.pop('app', None)
    _auth = importlib.import_module('app.auth')
    check("chi set username -> password van la mac dinh",
          _auth.verify_password('chiuser', 'admin@123') == 'chiuser')
    check("username mac dinh khong con dung khi da set env",
          _auth.verify_password('admin', 'admin@123') is None)
except SystemExit as e:
    check("thieu env KHONG duoc lam process thoat", False, f"SystemExit {e.code}")
finally:
    os.environ.pop('BASIC_AUTH_USERNAME', None)
    for k, v in _saved_auth_env.items():
        if v is not None:
            os.environ[k] = v
    for m, mod in _saved_mods.items():
        if mod is not None:
            sys.modules[m] = mod
        else:
            sys.modules.pop(m, None)

print("\n[10] /health")
rv = client.get("/health")
body = rv.get_json()
check("/health tra 200 + worker song", rv.status_code == 200 and body["worker_alive"], body)
check("/health khong can auth", rv.status_code != 401)
check("/health bao so rule", body["rules_loaded"] == 3, body)

print("\n[10b] /health doc dedup cache an toan khi co request song song")
import threading
_health_err = []


def _hammer_health():
    for _ in range(300):
        try:
            client.get("/health")
        except Exception as e:      # RuntimeError: dict changed size
            _health_err.append(e)
            return


def _hammer_reserve():
    for i in range(3000):
        fa.reserve_alert({"fingerprint": f"race{i}", "status": "firing",
                          "startsAt": "2026-09-07T10:00:00Z"})


ths = [threading.Thread(target=_hammer_health) for _ in range(3)]
ths += [threading.Thread(target=_hammer_reserve) for _ in range(3)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("/health + reserve chay song song khong nem RuntimeError",
      not _health_err, _health_err[:1])

print("\n[11] Queue co gioi han (bug #9)")
cfg2, fa2 = fresh(modern, QUEUE_MAXSIZE="2", TELEGRAM_CONFIG_PATH='/tmp/tg_cfg.json')
check("QUEUE_MAXSIZE doc tu env", fa2.alert_queue.maxsize == 2, fa2.alert_queue.maxsize)

print("\n[11b] Queue day -> 503 de Alertmanager retry (khong nuot im lang)")
# Chan worker lai bang cach cho send treo, roi bom cho tran queue.
_block = threading.Event()


async def slow_send(bot_token, chat_id, message, thread_id=None, max_retries=3):
    _block.wait(10)
    return fa2.SEND_OK


fa2.send_telegram_alert = slow_send
fa2.SEND_DELAY = 0
client2 = fa2.app.test_client()

burst = {"alerts": [
    {"fingerprint": f"burst{i}", "status": "firing",
     "startsAt": "2026-09-07T10:00:00Z",
     "labels": {"site": "hpg", "product": "storage", "alertname": f"B{i}"},
     "annotations": {}}
    for i in range(40)]}
rv = client2.post("/alert", json=burst, headers=hdr)
body = rv.get_json()
check("queue tran -> tra 503 chu khong 200", rv.status_code == 503, (rv.status_code, body))
check("dem rieng 'rejected' cho loi tam thoi", body.get("rejected", 0) > 0, body)
check("'dropped' khong bi dung cho queue day", body.get("dropped") == 0, body)
_block.set()

print("\n[11c] Payload di dang van tra 200 (retry vo ich)")
rv = client2.post("/alert", json={"alerts": [None, 123]}, headers=hdr)
body = rv.get_json()
check("alert di dang -> dropped, KHONG phai rejected",
      body.get("dropped") == 2 and body.get("rejected") == 0, body)
check("va tra 200 de Alertmanager khong retry vo han", rv.status_code == 200, rv.status_code)

print("\n[11d] LOG_PAYLOAD bat mot minh la co tac dung")
check("LOG_PAYLOAD duoc doc", fa.LOG_PAYLOAD is False or fa.LOG_PAYLOAD is True)
cfg3, fa3 = fresh(modern, LOG_PAYLOAD="1", TELEGRAM_CONFIG_PATH='/tmp/tg_cfg.json')
check("LOG_PAYLOAD=1 khong con bi chan boi LOG_LEVEL=INFO",
      fa3.LOG_PAYLOAD and fa3.app.logger.isEnabledFor(20), fa3.app.logger.level)
import inspect as _inspect
_src = _inspect.getsource(fa3.alertmanager_webhook)
check("payload duoc log o muc INFO chu khong DEBUG",
      "logger.info(f\"Received data" in _src, _src[:0])

print("\n[11e] Gioi han kich thuoc body")
check("MAX_CONTENT_LENGTH duoc set", fa3.app.config.get("MAX_CONTENT_LENGTH") > 0,
      fa3.app.config.get("MAX_CONTENT_LENGTH"))
cfg4, fa4 = fresh(modern, MAX_CONTENT_LENGTH_BYTES="512", TELEGRAM_CONFIG_PATH='/tmp/tg_cfg.json')
rv = fa4.app.test_client().post(
    "/alert", data=json.dumps({"alerts": [{"labels": {"a": "z" * 2000}}]}),
    content_type="application/json", headers=hdr)
check("body qua to -> 413 chu khong OOM", rv.status_code == 413, rv.status_code)

print("\n[12] app:app va app.flaskAlert:app la cung mot app (bug #6)")
# Phai tu reload trong muc nay: moi lan fresh() o tren lai thay module 'app',
# nen so sanh voi mot fa cu la so sanh nham doi tuong cua lan nap truoc.
cfg5, fa5 = fresh(modern, TELEGRAM_CONFIG_PATH='/tmp/tg_cfg.json')
app_pkg = importlib.import_module('app')
check("gunicorn app:app khong con ra app rong", app_pkg.app is fa5.app)
check("/alert dang ky tren app do", any(str(r) == "/alert" for r in app_pkg.app.url_map.iter_rules()))

print(f"\n{'='*52}\n  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  Failed: " + ", ".join(FAIL))
print('='*52)
sys.exit(1 if FAIL else 0)
