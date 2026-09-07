# prometheus-telegram-alert

Webhook receiver nhận alert từ Prometheus Alertmanager và gửi thông báo đến Telegram.

## Tính năng

- Nhận webhook từ Alertmanager, gửi alert đến Telegram qua Bot API
- **Multi-chat routing**: route alert sang bot/chat/topic khác nhau dựa theo labels
- **Dedup alert**: chặn alert trùng trong vòng 30 phút (TTLCache + MD5 hash)
- **Rate limiting**: queue background có giới hạn, throttle theo từng chat, tự retry khi bị Telegram flood control
- **Thread/Topic support**: hỗ trợ `message_thread_id` cho Telegram group có topics
- Basic Auth (so sánh hằng thời gian) bảo vệ webhook endpoint
- MarkdownV2 format với icon theo severity (critical 🚨 / warning ⚠️ / resolved ✅)
- Endpoint `/health` cho Docker HEALTHCHECK / liveness probe

---

## ⚠️ Thay đổi cần chú ý khi nâng cấp

1. **Basic Auth giờ fail-fast.** Nếu không set `BASIC_AUTH_USERNAME` và `BASIC_AUTH_PASSWORD`, container **sẽ không khởi động**. Muốn giữ hành vi cũ (dùng `admin`/`admin@123`), set thêm `ALLOW_INSECURE_AUTH=true` — không khuyến nghị cho production.
2. **`telegram_config.json` không còn được đóng gói vào image** (đã đưa vào `.dockerignore`). Phải mount lúc runtime. Thiếu file → chạy chế độ "không mapping", tất cả alert về DEFAULT bot.
3. **Routing chính xác hơn.** Rule dạng cũ giờ chỉ đối chiếu với value của những label nằm trong `PRIORITY_LABELS`. Trước đây một alert có `job="service"` cũng khớp rule `"service"` và bị gửi nhầm chat. Nếu bạn đang *dựa* vào hành vi cũ, hãy khai báo đủ label key vào `PRIORITY_LABELS`.
4. **Python 3.12, Flask 3, gunicorn 23.** Nâng gunicorn để vá CVE-2024-1135 (HTTP request smuggling).

---

## Cấu hình Alertmanager

Thêm receiver vào `alertmanager.yml`:

```yaml
receivers:
  - name: 'telegram-webhook'
    webhook_configs:
      - url: 'http://<IP_HOST>:9119/alert'
        send_resolved: true
        http_config:
          basic_auth:
            username: '<BASIC_AUTH_USERNAME>'
            password: '<BASIC_AUTH_PASSWORD>'
```

---

## Biến môi trường

| Biến | Bắt buộc | Mặc định | Mô tả |
|------|:--------:|----------|-------|
| `DEFAULT_BOT_TOKEN` | ✓ | — | Token bot Telegram mặc định |
| `DEFAULT_CHAT_ID` | ✓ | — | Chat ID nhận alert mặc định |
| `BASIC_AUTH_USERNAME` | ✓ | — | Username Basic Auth. Thiếu → không boot |
| `BASIC_AUTH_PASSWORD` | ✓ | — | Password Basic Auth. Thiếu → không boot |
| `DEFAULT_MESSAGE_THREAD_ID` | | — | Thread/Topic ID mặc định (group có topics) |
| `ALLOW_INSECURE_AUTH` | | `false` | `true` = chấp nhận `admin`/`admin@123` khi thiếu env auth |
| `TELEGRAM_CONFIG_PATH` | | `./telegram_config.json` | Đường dẫn file routing |
| `ALERT_REPEAT_INTERVAL` | | `1800` | TTL dedup (giây) |
| `ALERT_CACHE_MAXSIZE` | | `20000` | Số entry tối đa của cache dedup |
| `SEND_DELAY` | | `3` | Giây giữa 2 message vào cùng một chat |
| `QUEUE_MAXSIZE` | | `5000` | Giới hạn queue. Đầy → drop + log, Alertmanager retry |
| `MAX_BATCH` | | `200` | Số job xử lý tối đa trong một batch |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_PAYLOAD` | | `false` | `true` = log nguyên payload webhook (to, có thể lộ dữ liệu) |
| `ACCESS_LOG` | | tắt | `true` = bật access log của gunicorn |
| `GUNICORN_THREADS` | | `8` | Số thread |
| `GUNICORN_TIMEOUT` | | `120` | Timeout worker (giây) |

---

## File routing: `telegram_config.json`

Mount vào container tại `/prometheus-telegram-alert/telegram_config.json`.
Xem `telegram_config.example.json` làm mẫu.

> Đổi file này phải **restart container** — config chỉ được đọc một lần lúc boot.
> Key bắt đầu bằng `_` được bỏ qua hoàn toàn → dùng làm ghi chú (JSON không có cú pháp comment).

### Dạng khuyến nghị: `match` theo đúng label key

```json
{
  "PRIORITY_LABELS": ["site", "product"],

  "rules": {
    "storage-hpg": {
      "match": { "site": "hpg", "product": "storage" },
      "BOT_TOKEN": "111111:AAAA",
      "CHAT_ID": "-100111111",
      "MESSAGE_THREAD_ID": "5"
    },

    "network-all-sites": {
      "match": { "product": "network" },
      "BOT_TOKEN": "222222:BBBB",
      "CHAT_ID": "-100222222"
    }
  }
}
```

- `match` là tập điều kiện `label_key: label_value`, phải khớp **tất cả**
- Tên rule tự do — kể cả có dấu `-`, không ảnh hưởng gì đến việc match
- Rule nhiều điều kiện hơn được ưu tiên trước
- Không rule nào khớp → dùng `DEFAULT_BOT_TOKEN` / `DEFAULT_CHAT_ID`
- `MESSAGE_THREAD_ID` tùy chọn; bỏ qua → fallback `DEFAULT_MESSAGE_THREAD_ID`

### Gửi một alert đến nhiều group

Hai cách, dùng riêng hoặc kết hợp.

**Cách 1 — `targets`: một rule, nhiều đích**

```json
"ceph-rgw": {
  "match": { "service": "ceph", "role": "rgw" },
  "targets": [
    { "BOT_TOKEN": "111:AAA", "CHAT_ID": "-100111", "MESSAGE_THREAD_ID": "5" },
    { "BOT_TOKEN": "888:HHH", "CHAT_ID": "-100888" }
  ]
}
```

**Cách 2 — `continue`: nhiều rule cùng khớp** (giống `continue` của Alertmanager route)

```json
"noc-critical": {
  "match": { "severity": "critical" },
  "continue": true,
  "BOT_TOKEN": "999:III", "CHAT_ID": "-100999", "MESSAGE_THREAD_ID": "1"
},
"ceph-hpg": {
  "match": { "site": "hpg", "service": "ceph" },
  "BOT_TOKEN": "222:BBB", "CHAT_ID": "-100222"
}
```

Alert `severity=critical, site=hpg, service=ceph` → vào **cả** group NOC lẫn group Ceph.

- Rule `continue` **luôn được xét trước** mọi rule thường, bất kể số điều kiện — nếu không, một rule thường khớp trước sẽ dừng vòng lặp và rule `continue` không bao giờ chạy
- Vòng lặp dừng ở rule thường (không `continue`) đầu tiên khớp
- Đích trùng nhau (cùng `CHAT_ID` + `MESSAGE_THREAD_ID`) chỉ giữ một lần, không gửi đôi
- Target hỏng (thiếu `BOT_TOKEN`/`CHAT_ID`) bị loại lúc boot, các target còn lại vẫn chạy

**Dedup tính riêng cho từng đích.** Một alert fan-out ra 3 group có 3 dấu dedup độc lập: nếu group 3 gửi lỗi mạng, Alertmanager retry chỉ gửi lại group 3 — group 1 và 2 không bị trùng.

Kiểm tra trước khi deploy:

```bash
python tools/check_config.py telegram_config.json severity=critical site=hpg service=ceph
```

```
-> Khop rule: noc-critical, ceph-hpg
   [1] rule 'noc-critical'  CHAT_ID=-1009999999999  THREAD=1
   [2] rule 'ceph-hpg'      CHAT_ID=-1002222222222  THREAD=5
   => alert nay se duoc gui den 2 noi
```

### Dạng cũ (vẫn chạy được)

```json
{
  "PRIORITY_LABELS": ["site", "product", "service"],

  "service":       { "BOT_TOKEN": "...", "CHAT_ID": "..." },
  "site-provider": { "BOT_TOKEN": "...", "CHAT_ID": "..." }
}
```

- Key đơn (`"service"`): khớp khi **một label nằm trong `PRIORITY_LABELS`** có value `"service"`
- Key ghép (`"site-provider"`): khớp khi có cả value `"site"` và `"provider"` — hoặc khi có một label value đúng bằng `"site-provider"` nguyên khối
- Key ghép ưu tiên hơn key đơn

Hạn chế của dạng cũ: không phân biệt được label nào mang value đó, và tên rule không được chứa `-` một cách tự do. Nên chuyển dần sang `match`.

### Thứ tự ưu tiên

1. Rule có `"continue": true` — xét trước tất cả
2. Rule nhiều điều kiện hơn
3. Cùng số điều kiện → rule `match` thắng rule dạng cũ
4. Cùng cả ba → sắp theo tên rule (deterministic giữa các lần boot)

Rule thiếu `BOT_TOKEN`/`CHAT_ID`, hoặc `MESSAGE_THREAD_ID` không phải số, sẽ bị loại/chuẩn hoá ngay lúc boot và ghi log — không để nổ lúc đang xử lý webhook.

---

## Build image

```bash
docker build -t webhook-alert-telegram-python:3.0 .
```

## Deploy

```bash
docker run -d \
  --name telegram-bot \
  --restart always \
  -e DEFAULT_BOT_TOKEN="<telegram_bot_token>" \
  -e DEFAULT_CHAT_ID="<telegram_chat_id>" \
  -e DEFAULT_MESSAGE_THREAD_ID="<thread_id>" \
  -e BASIC_AUTH_USERNAME="myuser" \
  -e BASIC_AUTH_PASSWORD="mypassword" \
  -v /path/to/telegram_config.json:/prometheus-telegram-alert/telegram_config.json:ro \
  -p 9119:9119 \
  webhook-alert-telegram-python:3.0
```

### docker-compose

```yaml
services:
  telegram-bot:
    image: webhook-alert-telegram-python:3.0
    container_name: telegram-bot
    restart: always
    ports:
      - "9119:9119"
    environment:
      DEFAULT_BOT_TOKEN: "<telegram_bot_token>"
      DEFAULT_CHAT_ID: "<telegram_chat_id>"
      DEFAULT_MESSAGE_THREAD_ID: "<thread_id>"   # xóa nếu không dùng topics
      BASIC_AUTH_USERNAME: "myuser"
      BASIC_AUTH_PASSWORD: "mypassword"
    volumes:
      - ./telegram_config.json:/prometheus-telegram-alert/telegram_config.json:ro
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:9119/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

---

## Endpoint

| Method | Path | Auth | Mô tả |
|--------|------|:----:|-------|
| `POST` | `/alert` | ✓ | Nhận webhook Alertmanager. Trả `{queued, duplicates, dropped}` |
| `GET` | `/health` | — | Trạng thái worker, queue, cache, số rule đã nạp |

```bash
curl -s http://127.0.0.1:9119/health | jq
```

```json
{
  "status": "ok",
  "worker_alive": true,
  "queue_size": 0,
  "queue_maxsize": 5000,
  "dedup_cache_size": 12,
  "rules_loaded": 3,
  "default_bot_configured": true
}
```

`/health` trả `503` khi background worker chết → container tự restart nhờ HEALTHCHECK.

---

## Ghi chú vận hành

**Workers = 1 (cố định):** dedup dùng in-memory TTLCache. Tăng workers → mỗi worker có cache riêng và một background thread riêng, alert trùng sẽ bị gửi nhiều lần. Để scale horizontal, thay TTLCache bằng Redis (`SET NX EX`) — xem ghi chú trong `app/flaskAlert.py`.

**Webhook trả `200 OK` ngay:** việc gửi Telegram do background thread xử lý, không block request, tránh gunicorn timeout và Alertmanager retry trên batch chậm.

**Rate limiting:** worker giữ mốc thời gian gửi cuối cùng theo từng `(chat_id, thread_id)` và chờ bù đủ `SEND_DELAY` — kể cả giữa hai batch khác nhau. Các chat khác nhau được throttle độc lập, chạy song song.

**Xử lý lỗi gửi:**

| Loại lỗi | Hành động |
|----------|-----------|
| `RetryAfter` (flood control) | Chờ đúng `retry_after` rồi thử lại, tối đa 3 lần |
| `TimedOut` / `NetworkError` | Thử lại sau 3s, tối đa 3 lần |
| Hết retry | **Thả dấu dedup** → Alertmanager retry đi qua được |
| `BadRequest` / `Forbidden` / `InvalidToken` / `ChatMigrated` | **Giữ dấu dedup**, log ERROR, không retry (retry cũng fail y hệt) |

**Queue đầy** (`QUEUE_MAXSIZE`): drop alert + log ERROR + thả dấu dedup, thay vì phình bộ nhớ đến khi OOM.

**Message nhiều dòng:** annotation có xuống dòng được bọc trong pre-block ```` ``` ````; một dòng thì dùng inline code. MarkdownV2 không cho phép newline trong inline code — đây là nguyên nhân của lỗi `can't parse entities` khi description dài.

---

## Test

```bash
pip install -r requirements.txt
python tests/test_all.py      # 42 test
python tests/test_fanout.py   # 14 test fan-out
```

Kiểm tra config mà không cần cài gì (chỉ dùng thư viện chuẩn):

```bash
python tools/check_config.py telegram_config.json
python tools/check_config.py telegram_config.json site=dbp3 service=openstack
```

Exit code `1` khi có entry bị loại → cắm được vào CI hoặc chạy trước `docker restart`.

Bộ test dùng Telegram giả lập, không cần token thật: kiểm tra routing (cả hai schema), format MarkdownV2, dedup reserve/release, phân loại lỗi tạm thời vs vĩnh viễn, auth, `/health`, và input dị dạng.
