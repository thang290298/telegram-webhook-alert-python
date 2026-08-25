from flask import Flask, request, jsonify
from app.auth import auth
from config import *
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from dateutil import parser
from cachetools import TTLCache
from threading import Lock, Thread
from collections import defaultdict
from typing import NamedTuple
import os
import queue
import time
import pytz
import logging
import sys
import json
import asyncio
import re
from hashlib import md5

app = Flask(__name__)

# Logging config
log_formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
stdout_handler = logging.StreamHandler(sys.stdout)
stderr_handler = logging.StreamHandler(sys.stderr)

stdout_handler.setLevel(logging.DEBUG)
stderr_handler.setLevel(logging.ERROR)

stdout_handler.setFormatter(log_formatter)
stderr_handler.setFormatter(log_formatter)

app.logger.addHandler(stdout_handler)
app.logger.addHandler(stderr_handler)
app.logger.setLevel(logging.DEBUG)

# ================== CHONG ALERT TRUNG ==================
# TTLCache tu dong xoa entry sau ALERT_REPEAT_INTERVAL giay - khong bi memory leak
# Lock dam bao thread-safe khi gunicorn dung nhieu threads
ALERT_REPEAT_INTERVAL = int(os.environ.get('ALERT_REPEAT_INTERVAL', 1800))  # giay (mac dinh 30 phut)
ALERT_CACHE_MAXSIZE = int(os.environ.get('ALERT_CACHE_MAXSIZE', 20000))
recent_alerts_cache = TTLCache(maxsize=ALERT_CACHE_MAXSIZE, ttl=ALERT_REPEAT_INTERVAL)
cache_lock = Lock()

# ================== BACKGROUND SEND QUEUE ==================
# Delay between messages to same chat to respect Telegram rate limit (20 msg/min/group)
SEND_DELAY = 3  # seconds

class AlertJob(NamedTuple):
    """Mot don vi cong viec trong queue.
    Dung NamedTuple thay tuple tran de khong vo khi unpack luc them field."""
    bot_token: str
    chat_id: str
    message: str
    thread_id: object
    alert_hash: str


# background_worker drains the queue so HTTP requests return 200 immediately
alert_queue = queue.Queue()


def background_worker():
    """Drain alert_queue in a background thread.
    Groups by chat, sends sequentially per chat with SEND_DELAY.
    Never lets the thread die silently on unexpected errors."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        # Collect all currently queued items (non-blocking drain)
        items = []
        try:
            while True:
                items.append(alert_queue.get_nowait())
        except queue.Empty:
            pass

        if not items:
            # No items -- sleep briefly before next poll (plain thread sleep, no asyncio overhead)
            time.sleep(0.5)
            continue

        try:
            # Group by (chat_id, thread_id) so each chat is throttled independently
            chat_groups = defaultdict(list)
            for job in items:
                chat_groups[(job.chat_id, job.thread_id)].append(job)

            async def send_all_groups(groups):
                async def send_group(group_items):
                    for i, job in enumerate(group_items):
                        ok = await send_telegram_alert(
                            job.bot_token, job.chat_id, job.message, job.thread_id
                        )
                        if not ok:
                            # Go dau dedup -> lan retry cua Alertmanager di qua duoc,
                            # thay vi bi nuot im lang trong ALERT_REPEAT_INTERVAL.
                            release_alert(job.alert_hash)
                        if i < len(group_items) - 1:
                            await asyncio.sleep(SEND_DELAY)

                await asyncio.gather(*[send_group(g) for g in groups.values()])

            loop.run_until_complete(send_all_groups(chat_groups))

        except Exception as e:
            # Log and continue -- never let the worker thread die silently
            app.logger.error(f"background_worker error: {e}", exc_info=True)
            # Ca batch coi nhu that bai -> tha dau dedup ra.
            # Danh doi co y: tha trung con hon mat alert.
            for job in items:
                release_alert(job.alert_hash)


# Start background worker thread (daemon=True so it exits with main process)
_worker_thread = Thread(target=background_worker, daemon=True)
_worker_thread.start()
# ===========================================================


def _alert_identity(alert):
    """Dinh danh duy nhat cua 1 alert.
    Uu tien 'fingerprint' cua Alertmanager - la hash cua TOAN BO label set.
    Fallback: serialize toan bo labels, de khong bo sot label nao
    (vd mountpoint / device / pod / target) khien 2 alert khac nhau
    bi coi la trung va bi nuot."""
    fingerprint = alert.get('fingerprint')
    if fingerprint:
        return f"fp:{fingerprint}"
    return "lb:" + json.dumps(alert.get('labels', {}), sort_keys=True)


def build_alert_hash(alert):
    """Tra ve (alert_hash, key_fields).
    Key gom: identity (full labels) + status + timestamp cua vong doi tuong ung
    -> chan repeat cua Alertmanager, nhung van cho flapping va resolved di qua."""
    status = alert.get('status', '').lower()
    raw_ts = alert.get('startsAt') if status == 'firing' else alert.get('endsAt')

    ts = None
    if raw_ts:
        try:
            ts = round(parser.parse(raw_ts).timestamp())
        except Exception as e:
            app.logger.warning(f"Failed to parse timestamp '{raw_ts}': {e}")

    key_fields = {
        'identity': _alert_identity(alert),
        'status': status,
        'timestamp': ts
    }
    alert_hash = md5(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()
    return alert_hash, key_fields


def reserve_alert(alert):
    """Tra ve (is_duplicate, alert_hash).
    Danh dau alert ngay khi nhan de chan trung trong cung 1 burst;
    neu gui that bai, background_worker goi release_alert() de tha ra."""
    alert_hash, key_fields = build_alert_hash(alert)

    with cache_lock:
        if alert_hash in recent_alerts_cache:
            app.logger.info(
                f"Duplicate alert ({key_fields['status']}) skipped: {key_fields['identity']}"
            )
            return True, alert_hash

        recent_alerts_cache[alert_hash] = True

        if len(recent_alerts_cache) >= ALERT_CACHE_MAXSIZE * 0.9:
            app.logger.warning(
                f"Dedup cache near capacity ({len(recent_alerts_cache)}/{ALERT_CACHE_MAXSIZE})"
                " - entries may be evicted before TTL, duplicates can slip through"
            )

    return False, alert_hash


def release_alert(alert_hash):
    """Go dau dedup khi gui that bai, de Alertmanager retry di qua duoc."""
    if not alert_hash:
        return
    with cache_lock:
        recent_alerts_cache.pop(alert_hash, None)


# De scale nhieu worker / giu cache qua restart, thay TTLCache bang Redis:
#   reserve: redis_client.set(alert_hash, "1", nx=True, ex=ALERT_REPEAT_INTERVAL) is not None
#   release: redis_client.delete(alert_hash)
# SET NX EX la atomic nen bo luon duoc cache_lock, va gunicorn.conf.py
# khi do moi bo duoc rang buoc workers=1.

# ================================================================


def escape_md2(text):
    """Escape special chars for Telegram MarkdownV2 plain text."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-\\])', r'\\\1', str(text))


def safe_code(text):
    """Prepare text for inline code span in MarkdownV2.
    Replace backtick with single-quote to avoid breaking the code span."""
    return str(text).replace('`', "'").replace('\\', '\\\\')


def format_telegram_message(alert, labels, annotations):
    status = alert['status'].lower()
    severity = labels.get('severity', '').lower()
    status_text = alert['status'].upper()

    if status == "resolved":
        status_icon = "✅"
        alertname_icon = "✅"
        alertname_suffix = ""
    elif severity == "critical":
        status_icon = "\U0001f6a8"
        alertname_icon = "\U0001f6a8"
        alertname_suffix = "\U0001f6a8\U0001f6a8\U0001f6a8"
    elif severity == "warning":
        status_icon = "⚠️"
        alertname_icon = "⚠️"
        alertname_suffix = ""
    else:
        status_icon = "ℹ️"
        alertname_icon = "ℹ️"
        alertname_suffix = ""

    alertname = escape_md2(labels.get('alertname', 'N/A'))

    message_lines = [
        f"*Status:* {status_icon} {status_text} {status_icon}",
        f"*Alertname:* {alertname_icon} {alertname}{alertname_suffix}"
    ]

    if 'info' in annotations:
        message_lines.append(f"*Info:* `{safe_code(annotations['info'])}`")
    if 'summary' in annotations:
        message_lines.append(f"*Summary:* `{safe_code(annotations['summary'])}`")
    if 'description' in annotations:
        message_lines.append(f"*Description:* `{safe_code(annotations['description'])}`")

    try:
        if status == "resolved":
            correct_date = parser.parse(alert['endsAt']).astimezone(
                pytz.timezone('Asia/Bangkok')
            ).strftime('%Y-%m-%d %H:%M:%S')
            message_lines.append(f"*Resolved:* `{correct_date}`")
        elif status == "firing":
            correct_date = parser.parse(alert['startsAt']).astimezone(
                pytz.timezone('Asia/Bangkok')
            ).strftime('%Y-%m-%d %H:%M:%S')
            message_lines.append(f"*Started:* `{correct_date}`")
    except Exception as e:
        message_lines.append(f"*Date parse error:* `{safe_code(str(e))}`")

    return '\n'.join(message_lines)


async def send_telegram_alert(bot_token, chat_id, message, thread_id=None, max_retries=3):
    """Send alert to Telegram. Retry up to max_retries times using a loop (no recursion)."""
    if not bot_token or not chat_id:
        app.logger.error("bot_token or chat_id is None - Skip sending message")
        return False

    # Validate thread_id before entering retry loop to avoid uncaught ValueError
    thread_id_int = None
    if thread_id is not None:
        try:
            thread_id_int = int(thread_id)
        except (ValueError, TypeError) as e:
            app.logger.error(f"Invalid MESSAGE_THREAD_ID '{thread_id}': {e} - sending without thread")

    bot = Bot(token=bot_token)
    send_kwargs = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': ParseMode.MARKDOWN_V2
    }
    if thread_id_int is not None:
        send_kwargs['message_thread_id'] = thread_id_int

    for attempt in range(1, max_retries + 1):
        try:
            await bot.send_message(**send_kwargs)
            app.logger.info(f"Sent alert to chat_id {chat_id} success")
            return True

        except RetryAfter as e:
            wait_time = e.retry_after
            app.logger.warning(
                f"Flood control exceeded. Retry in {wait_time}s "
                f"(attempt {attempt}/{max_retries})"
            )
            if attempt < max_retries:
                await asyncio.sleep(wait_time)

        except (TimedOut, NetworkError) as e:
            app.logger.warning(
                f"Network/Timeout error: {e}. Retry in 3s "
                f"(attempt {attempt}/{max_retries})"
            )
            if attempt < max_retries:
                await asyncio.sleep(3)

        except Exception as e:
            app.logger.error(f"Failed to send alert: {str(e)}")
            return False

    app.logger.error(f"Failed to send alert to chat_id {chat_id} after {max_retries} attempts")
    return False


def find_mapping_from_labels(labels):
    label_values = set(labels.values())

    for key in sorted(TELEGRAM_CONFIG.keys(), key=lambda x: -len(x.split('-'))):
        parts = key.split('-')
        if all(part in label_values for part in parts):
            app.logger.info(f"Mapping found for multi-label key={key}")
            return TELEGRAM_CONFIG[key]

    for label in PRIORITY_LABELS:
        label_value = labels.get(label)
        if label_value and label_value in TELEGRAM_CONFIG:
            app.logger.info(f"Mapping found for {label}={label_value}")
            return TELEGRAM_CONFIG[label_value]

    app.logger.warning(f"No mapping found for labels: {labels}. Use DEFAULT BOT TOKEN")
    return None


@app.route('/alert', methods=['POST'])
@auth.login_required
def alertmanager_webhook():
    data = request.json

    if not data or 'alerts' not in data:
        app.logger.error("Invalid request - no alerts found")
        return jsonify({'status': 'error', 'message': 'Invalid request'}), 400

    app.logger.info("Webhook called")
    app.logger.debug(f"Received data: {json.dumps(data)}")

    # Enqueue alerts for background processing -- return 200 immediately.
    # background_worker() drains the queue with rate limiting (SEND_DELAY between msgs).
    # This prevents Gunicorn timeout and AlertManager retries on slow batches.
    queued = 0
    for alert in data['alerts']:
        # reserve_alert() da log chi tiet khi trung -> khong log lap o day
        is_dup, alert_hash = reserve_alert(alert)
        if is_dup:
            continue

        labels = alert.get('labels', {})
        annotations = alert.get('annotations', {})

        bot_token = DEFAULT_BOT_TOKEN
        chat_id = DEFAULT_CHAT_ID
        thread_id = DEFAULT_MESSAGE_THREAD_ID

        mapping = find_mapping_from_labels(labels)

        if mapping:
            bot_token = mapping['BOT_TOKEN']
            chat_id = mapping['CHAT_ID']
            thread_id = mapping.get('MESSAGE_THREAD_ID', thread_id)

        message = format_telegram_message(alert, labels, annotations)
        alert_queue.put(AlertJob(bot_token, chat_id, message, thread_id, alert_hash))
        queued += 1

    app.logger.info(f"Queued {queued} alert(s) for background sending")

    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9119)
