# prometheus-telegram-alert

Webhook receiver nhận alert từ Prometheus Alertmanager và gửi thông báo đến Telegram.

## Tính năng

- Nhận webhook từ Alertmanager, gửi alert đến Telegram qua Bot API
- **Multi-chat routing**: tự động route alert sang bot/chat/topic khác nhau dựa theo labels
- **Dedup alert**: chặn alert trùng trong vòng 30 phút (TTLCache + MD5 hash)
- **Rate limiting**: queue background, tự retry khi bị Telegram flood control
- **Thread/Topic support**: hỗ trợ `message_thread_id` cho Telegram group có topics
- Basic Auth bảo vệ webhook endpoint
- MarkdownV2 format với icon theo severity (critical 🚨 / warning ⚠️ / resolved ✅)

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
            username: 'admin'
            password: 'admin@123'
```

---

## Biến môi trường

| Biến | Bắt buộc | Mặc định | Mô tả |
|------|:--------:|----------|-------|
| `DEFAULT_BOT_TOKEN` | ✓ | — | Token bot Telegram mặc định |
| `DEFAULT_CHAT_ID` | ✓ | — | Chat ID nhận alert mặc định |
| `DEFAULT_MESSAGE_THREAD_ID` | | — | Thread/Topic ID mặc định (chỉ dùng cho group có topics) |
| `BASIC_AUTH_USERNAME` | | `admin` | Username Basic Auth cho webhook endpoint |
| `BASIC_AUTH_PASSWORD` | | `admin@123` | Password Basic Auth cho webhook endpoint |

> **Lưu ý:** Nếu không set `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD`, bot dùng credentials mặc định và in cảnh báo ra stderr. Không nên dùng mặc định trên môi trường production.

---

## File cần mount

### `telegram_config.json`

Mount vào container tại: `/prometheus-telegram-alert/telegram_config.json`

File này định nghĩa rule routing alert theo labels. Nếu không mount, bot dùng file mặc định trong image (chứa token placeholder, sẽ lỗi khi gửi).

**Cấu trúc:**

```json
{
  "PRIORITY_LABELS": ["site", "product", "service"],

  "service": {
    "BOT_TOKEN": "<bot_token>",
    "CHAT_ID": "<chat_id>",
    "MESSAGE_THREAD_ID": "<thread_id>"
  },

  "site-provider": {
    "BOT_TOKEN": "<bot_token>",
    "CHAT_ID": "<chat_id>",
    "MESSAGE_THREAD_ID": "<thread_id>"
  }
}
```

**Giải thích routing:**

- `PRIORITY_LABELS`: danh sách label key được dùng để match, theo thứ tự ưu tiên
- Key đơn (`"service"`): match khi alert có label value = `"service"`
- Key ghép (`"site-provider"`): match khi alert có **cả hai** label value `"site"` **và** `"provider"` cùng lúc (nối bằng `-`)
- Key ghép có độ ưu tiên cao hơn key đơn
- Nếu không match rule nào → dùng `DEFAULT_BOT_TOKEN` / `DEFAULT_CHAT_ID`
- `MESSAGE_THREAD_ID` trong từng rule là tùy chọn; nếu bỏ qua, fallback về `DEFAULT_MESSAGE_THREAD_ID`

---

## Build image

```bash
git clone <repo_url>
cd prometheus-telegram-alert

docker build -t webhook-alert-telegram-python:2.2 .
```

---

## Deploy container

### Tối thiểu (chỉ dùng default bot)

```bash
docker run -d \
  --name telegram-bot \
  -e DEFAULT_BOT_TOKEN="<telegram_bot_token>" \
  -e DEFAULT_CHAT_ID="<telegram_chat_id>" \
  -p 9119:9119 \
  webhook-alert-telegram-python:2.2
```

### Đầy đủ (custom auth + thread + routing)

```bash
docker run -d \
  --name telegram-bot \
  -e DEFAULT_BOT_TOKEN="<telegram_bot_token>" \
  -e DEFAULT_CHAT_ID="<telegram_chat_id>" \
  -e DEFAULT_MESSAGE_THREAD_ID="<thread_id>" \
  -e BASIC_AUTH_USERNAME="myuser" \
  -e BASIC_AUTH_PASSWORD="mypassword" \
  -v /path/to/telegram_config.json:/prometheus-telegram-alert/telegram_config.json:ro \
  -p 9119:9119 \
  webhook-alert-telegram-python:2.2
```

### docker-compose

```yaml
services:
  telegram-bot:
    image: webhook-alert-telegram-python:2.2
    container_name: telegram-bot
    restart: always
    ports:
      - "9119:9119"
    environment:
      DEFAULT_BOT_TOKEN: "<telegram_bot_token>"
      DEFAULT_CHAT_ID: "<telegram_chat_id>"
      DEFAULT_MESSAGE_THREAD_ID: "<thread_id>"   # xóa dòng này nếu không dùng topics
      BASIC_AUTH_USERNAME: "myuser"
      BASIC_AUTH_PASSWORD: "mypassword"
    volumes:
      - ./telegram_config.json:/prometheus-telegram-alert/telegram_config.json:ro
```

---

## Ví dụ `telegram_config.json`

```json
{
  "PRIORITY_LABELS": ["env", "team"],

  "production": {
    "BOT_TOKEN": "111111:AAAA",
    "CHAT_ID": "-100111111",
    "MESSAGE_THREAD_ID": "5"
  },

  "staging": {
    "BOT_TOKEN": "222222:BBBB",
    "CHAT_ID": "-100222222"
  },

  "production-backend": {
    "BOT_TOKEN": "333333:CCCC",
    "CHAT_ID": "-100333333",
    "MESSAGE_THREAD_ID": "12"
  }
}
```

- Alert có label `env=production` và `team=backend` → match `"production-backend"` (key ghép ưu tiên hơn)
- Alert có label `env=production` → match `"production"`
- Alert có label `env=staging` → match `"staging"`
- Không match → dùng `DEFAULT_BOT_TOKEN` / `DEFAULT_CHAT_ID`

---

## Ghi chú vận hành

**Workers = 1 (cố định):** `gunicorn.conf.py` cấu hình `workers=1` vì dedup dùng in-memory TTLCache. Nếu tăng workers, mỗi worker có cache riêng và alert trùng có thể bị gửi nhiều lần. Để scale horizontal, cần thay TTLCache bằng Redis.

**Threads = 8, timeout = 120s:** Webhook trả về `200 OK` ngay lập tức; việc gửi Telegram được xử lý bởi background thread riêng, không block request.

**Rate limiting tự động:** Background worker delay 3 giây giữa các message trong cùng một chat, tự retry khi Telegram trả về `RetryAfter`, để không vượt giới hạn 20 msg/phút của Telegram group.
