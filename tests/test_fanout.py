"""Test fan-out: mot alert gui den nhieu group. Chay: python tests/test_fanout.py"""
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
    path = '/tmp/tg_fanout.json'
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
    return importlib.import_module('config'), importlib.import_module('app.flaskAlert')


FANOUT = {
    "PRIORITY_LABELS": ["site", "service", "severity"],
    "rules": {
        "noc-critical": {
            "match": {"severity": "critical"},
            "continue": True,
            "BOT_TOKEN": "T_NOC", "CHAT_ID": "C_NOC", "MESSAGE_THREAD_ID": "1"
        },
        "ceph-team": {
            "match": {"service": "ceph"},
            "targets": [
                {"BOT_TOKEN": "T_A", "CHAT_ID": "C_TEAM", "MESSAGE_THREAD_ID": "9"},
                {"BOT_TOKEN": "T_B", "CHAT_ID": "C_ONCALL"}
            ]
        },
        "trung-dich": {
            "match": {"site": "zz"},
            "targets": [
                {"BOT_TOKEN": "T_A", "CHAT_ID": "C_SAME", "MESSAGE_THREAD_ID": "2"},
                {"BOT_TOKEN": "T_B", "CHAT_ID": "C_SAME", "MESSAGE_THREAD_ID": "2"}
            ]
        },
        "targets-hong": {
            "match": {"site": "bad"},
            "targets": [
                {"BOT_TOKEN": "T", "CHAT_ID": "C_OK"},
                {"BOT_TOKEN": "T"},
                "khong phai object"
            ]
        },
    },
}

print("\n[1] resolve_destinations")
cfg, fa = fresh(FANOUT)

d = fa.resolve_destinations({"service": "ceph"})
check("1 rule co 2 targets -> 2 dich", len(d) == 2, d)
check("target thu 2 khong khai thread -> None", d[1][2] is None, d)

d = fa.resolve_destinations({"severity": "critical", "service": "ceph"})
check("continue:true -> gop dich cua ca 2 rule",
      [x[1] for x in d] == ["C_NOC", "C_TEAM", "C_ONCALL"], [x[1] for x in d])

d = fa.resolve_destinations({"service": "ceph", "site": "khac"})
check("khong continue -> dung o rule dau tien", len(d) == 2 and d[0][1] == "C_TEAM", d)

d = fa.resolve_destinations({"site": "zz"})
check("dich trung nhau (cung chat+topic) bi loai", len(d) == 1, d)

d = fa.resolve_destinations({"site": "bad"})
check("target hong bi loai, target tot van chay", [x[1] for x in d] == ["C_OK"], d)

d = fa.resolve_destinations({"site": "khong-khop-gi"})
check("khong khop -> DEFAULT bot", len(d) == 1 and d[0][1] == "-100999", d)

print("\n[2] End-to-end + dedup theo tung dich")
sent, results = [], {}


async def fake_send(bot_token, chat_id, message, thread_id=None, max_retries=3):
    sent.append(chat_id)
    return results.get(chat_id, fa.SEND_OK)


fa.send_telegram_alert = fake_send
fa.SEND_DELAY = 0
client = fa.app.test_client()
hdr = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
payload = {"alerts": [{"fingerprint": "fan1", "status": "firing",
                       "startsAt": "2026-09-07T10:00:00Z",
                       "labels": {"severity": "critical", "service": "ceph", "alertname": "X"},
                       "annotations": {"summary": "test"}}]}


def wait(n, timeout=5):
    t0 = time.time()
    while len(sent) < n and time.time() - t0 < timeout:
        time.sleep(0.05)
    time.sleep(0.3)


results["C_ONCALL"] = fa.SEND_RETRY          # 1 group gap loi tam thoi
rv = client.post("/alert", json=payload, headers=hdr)
check("1 alert -> queue 3 job", rv.get_json()["queued"] == 3, rv.get_json())
wait(3)
check("gui du 3 group", sorted(sent) == ["C_NOC", "C_ONCALL", "C_TEAM"], sent)

results.clear()
sent.clear()
rv = client.post("/alert", json=payload, headers=hdr)
body = rv.get_json()
check("chi group gui loi duoc retry, 2 group kia van bi dedup",
      (body["queued"], body["duplicates"]) == (1, 2), body)
wait(1)
check("va dung group do duoc gui lai", sent == ["C_ONCALL"], sent)

sent.clear()
rv = client.post("/alert", json=payload, headers=hdr)
check("lan 3: ca 3 dich deu da gui xong -> dedup het",
      rv.get_json()["duplicates"] == 3, rv.get_json())

print("\n[3] Tuong thich nguoc: config 1 dich van chay nhu cu")
cfg, fa = fresh({"PRIORITY_LABELS": ["site"],
                 "rules": {"a": {"match": {"site": "x"},
                                 "BOT_TOKEN": "T", "CHAT_ID": "C", "MESSAGE_THREAD_ID": "3"}}})
d = fa.resolve_destinations({"site": "x"})
check("rule 1 dich -> 1 destination", d == [("T", "C", "3")], d)

cfg, fa = fresh({"PRIORITY_LABELS": ["site"],
                 "hpg": {"BOT_TOKEN": "T2", "CHAT_ID": "C2"}})
d = fa.resolve_destinations({"site": "hpg"})
check("schema cu van chay", d == [("T2", "C2", None)], d)

print(f"\n{'='*52}\n  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  Failed: " + ", ".join(FAIL))
print('='*52)
sys.exit(1 if FAIL else 0)
