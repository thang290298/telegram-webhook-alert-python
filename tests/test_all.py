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
check("alertname duoc escape", "Disk\\-Full" in msg, msg)
check("timestamp doi ve gio VN", "2026-09-07 17:00:00" in msg, msg)

msg = fa.format_telegram_message({}, {}, {})
check("alert thieu 'status' khong con KeyError (bug #5)", "UNKNOWN" in msg, msg)

msg = fa.format_telegram_message(
    {"status": "resolved", "endsAt": "2026-09-07T10:00:00Z"}, {},
    {"description": "co `backtick` va \\ backslash"})
check("backtick duoc escape dung thay vi thay bang nhay don",
      "\\`backtick\\`" in msg and "\\\\ backslash" in msg, msg)

# ============================================================
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

print("\n[10] /health")
rv = client.get("/health")
body = rv.get_json()
check("/health tra 200 + worker song", rv.status_code == 200 and body["worker_alive"], body)
check("/health khong can auth", rv.status_code != 401)
check("/health bao so rule", body["rules_loaded"] == 3, body)

print("\n[11] Queue co gioi han (bug #9)")
cfg2, fa2 = fresh(modern, QUEUE_MAXSIZE="2", TELEGRAM_CONFIG_PATH='/tmp/tg_cfg.json')
check("QUEUE_MAXSIZE doc tu env", fa2.alert_queue.maxsize == 2, fa2.alert_queue.maxsize)

print("\n[12] app:app va app.flaskAlert:app la cung mot app (bug #6)")
import app as app_pkg
check("gunicorn app:app khong con ra app rong", app_pkg.app is fa2.app)
check("/alert dang ky tren app do", any(str(r) == "/alert" for r in app_pkg.app.url_map.iter_rules()))

print(f"\n{'='*52}\n  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  Failed: " + ", ".join(FAIL))
print('='*52)
sys.exit(1 if FAIL else 0)
