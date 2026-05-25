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
import queue
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
ALERT_REPEAT_INTERVAL = 1800  # giay (30 phut)
recent_alerts_cache = TTLCache(maxsize=2000, ttl=ALERT_REPEAT_INTERVAL)
cache_lock = Lock()

# ================== BACKGROUND SEND QUEUE ==================
# Delay between messages to same chat to respect Telegram rate limit (20 msg/min/group)
SEND_DELAY = 3  # seconds

# Queue holds (bot_token, chat_id, message, thread_id) tuples grouped by chat key
# background_worker drains the queue so HTTP requests return 200 immediately
alert_queue = queue.Queue()


def background_worker():
    """Drain alert_queue in a background thread.
    Groups by chat, sends sequentially per chat with SEND_DELAY."""
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

        if items:
            # Group by (chat_id, thread_id)
            chat_groups = defaultdict(list)
            for item in items:
                _, chat_id, _, thread_id = item
                chat_groups[(chat_id, thread_id)].append(item)

            async def send_all_groups():
                async def send_group(group_items):
                    for i, (bot_token, chat_id, message, thread_id) in enumerate(group_items):
                        await send_telegram_alert(bot_token, chat_id, message, thread_id)
                        if i < len(group_items) - 1:
                            await asyncio.sleep(SEND_DELAY)

                await asyncio.gather(*[send_group(g) for g in chat_groups.values()])

            loop.run_until_complete(send_all_groups())
        else:
            # No items — sleep briefly before next poll
            loop.run_until_complete(asyncio.sleep(0.5))


# Start background worker thread (daemon=True so it exits with main process)
_worker_thread = Thread(target=background_worker, daemon=True)
_worker_thread.start()
# ===========================================================


def is_duplicate_alert(alert):
    labels = alert.get('labels', {})
    status = alert.get('status', '').lower()

    try:
        starts_at_ts = round(parser.parse(alert.get('startsAt', '')).timestamp()) if 'startsAt' in alert else None
        ends_at_ts = round(parser.parse(alert.get('endsAt', '')).timestamp()) if 'endsAt' in alert else None
    except Exception as e:
        app.logger.warning(f"Failed to parse timestamps: {e}")
        starts_at_ts = None
        ends_at_ts = None

    alert_key_fields = {
        'severity': labels.get('severity'),
        'instance': labels.get('instance'),
        'alertname': labels.get('alertname'),
        'job': labels.get('job'),
        'status': status,
        'timestamp': starts_at_ts if status == 'firing' else ends_at_ts
    }

    alert_id = json.dumps(alert_key_fields, sort_keys=True)
    alert_hash = md5(alert_id.encode()).hexdigest()

    with cache_lock:
        if alert_hash in recent_alerts_cache:
            app.logger.info(f"Duplicate alert ({status}) detected: {alert_key_fields}")
            return True
        recent_alerts_cache[alert_hash] = True
    return False

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
        status_icon = "🚨"
        alertname_icon = "🚨"
        alertname_suffix = "🚨🚨🚨"
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

    # Enqueue alerts for background processing — return 200 immediately.
    # background_worker() drains the queue with rate limiting (SEND_DELAY between msgs).
    # This prevents Gunicorn timeout and AlertManager retries on slow batches.
    queued = 0
    for alert in data['alerts']:
        if is_duplicate_alert(alert):
            app.logger.info("Duplicate alert detected. Skip sending.")
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
        alert_queue.put((bot_token, chat_id, message, thread_id))
        queued += 1

    app.logger.info(f"Queued {queued} alert(s) for background sending")

    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9119)
