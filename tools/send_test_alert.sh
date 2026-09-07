#!/usr/bin/env bash
# Ban alert thu den webhook, khong can Prometheus/Alertmanager.
#
#   ./tools/send_test_alert.sh                 # ban tat ca cac mau
#   ./tools/send_test_alert.sh critical        # chi mau critical
#   ./tools/send_test_alert.sh resolved
#
# Cau hinh qua bien moi truong:
#   URL=http://127.0.0.1:9119/alert  USER=alertmgr  PASS=xxx  ./tools/send_test_alert.sh

set -euo pipefail

URL="${URL:-http://127.0.0.1:9119/alert}"
USER="${USER:-alertmgr}"
PASS="${PASS:-}"
WHICH="${1:-all}"

if [ -z "$PASS" ]; then
    echo "Thieu mat khau. Chay:  PASS='<mat_khau>' $0 $WHICH" >&2
    exit 1
fi

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# fingerprint khac nhau moi lan chay -> khong bi dedup chan
RUN_ID="$(date +%s)"

send() {
    local ten="$1" body="$2"
    printf '\n--- %s ---\n' "$ten"
    curl -sS -u "$USER:$PASS" -X POST "$URL" \
         -H 'Content-Type: application/json' -d "$body"
    printf '\n'
}

# ---------------------------------------------------------------- critical
critical() {
send "critical / description nhieu dong" "$(cat <<EOF
{"alerts":[{
  "fingerprint":"test-crit-$RUN_ID",
  "status":"firing",
  "startsAt":"$NOW",
  "labels":{
    "alertname":"Out of Memory",
    "severity":"critical",
    "site":"dbp3",
    "service":"openstack",
    "instance":"10.195.16.206:9012"
  },
  "annotations":{
    "summary":"Out of Memory on node server",
    "description":"\n - Memory use: 96.54031500044677%\n - level: critical\n - instance: 10.195.16.206:9012\n - hostname:compute26-dbp3\n - job: server_ops_dbp3"
  }
}]}
EOF
)"
}

# ---------------------------------------------------------------- warning
warning() {
send "warning / description 1 dong" "$(cat <<EOF
{"alerts":[{
  "fingerprint":"test-warn-$RUN_ID",
  "status":"firing",
  "startsAt":"$NOW",
  "labels":{
    "alertname":"CephOSDNearFull",
    "severity":"warning",
    "site":"hpg",
    "service":"ceph"
  },
  "annotations":{
    "summary":"OSD.12 dat 82% dung luong",
    "description":"osd.12 tren node ceph-hpg-03 dang o muc 82%"
  }
}]}
EOF
)"
}

# ---------------------------------------------------------------- resolved
resolved() {
send "resolved" "$(cat <<EOF
{"alerts":[{
  "fingerprint":"test-crit-$RUN_ID",
  "status":"resolved",
  "startsAt":"$NOW",
  "endsAt":"$NOW",
  "labels":{
    "alertname":"Out of Memory",
    "severity":"critical",
    "site":"dbp3",
    "service":"openstack"
  },
  "annotations":{
    "summary":"Out of Memory on node server",
    "description":"\n - hostname:compute26-dbp3\n - job: server_ops_dbp3"
  }
}]}
EOF
)"
}

# ---------------------------------------------------------------- dedup
dedup() {
    printf '\n--- dedup: ban 2 lan cung mot alert ---\n'
    local body
    body="$(cat <<EOF
{"alerts":[{
  "fingerprint":"test-dedup-$RUN_ID",
  "status":"firing",
  "startsAt":"$NOW",
  "labels":{"alertname":"DedupTest","severity":"warning","site":"hpg"},
  "annotations":{"summary":"Alert nay chi duoc gui 1 lan"}
}]}
EOF
)"
    printf 'lan 1: '; curl -sS -u "$USER:$PASS" -X POST "$URL" -H 'Content-Type: application/json' -d "$body"; printf '\n'
    printf 'lan 2: '; curl -sS -u "$USER:$PASS" -X POST "$URL" -H 'Content-Type: application/json' -d "$body"; printf '\n'
    printf '=> lan 2 phai tra "duplicates":1 va Telegram KHONG nhan them tin\n'
}

case "$WHICH" in
    critical) critical ;;
    warning)  warning ;;
    resolved) resolved ;;
    dedup)    dedup ;;
    all)      critical; warning; resolved; dedup ;;
    *) echo "Khong hieu '$WHICH'. Chon: critical | warning | resolved | dedup | all" >&2; exit 2 ;;
esac

printf '\nXong. Kiem tra Telegram va:  docker logs --tail 30 telegram-bot\n'
