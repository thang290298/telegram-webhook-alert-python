import asyncio
import atexit
import json
import logging
import os
import queue
import re
import sys
import time
from collections import defaultdict
from hashlib import md5
from threading import Lock, Thread
from typing import NamedTuple

import pytz
from cachetools import TTLCache
from dateutil import parser
from flask import jsonify, request
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from app import app
from app.auth import auth
from config import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    DEFAULT_MESSAGE_THREAD_ID,
    RULES,
    find_rules,
)

# ================== LOGGING ==================
# Truoc day stdout_handler (level DEBUG) va stderr_handler (level ERROR) cung
# gan vao app.logger -> moi dong ERROR bi in HAI lan. Gio stdout chi nhan
# < ERROR, stderr nhan >= ERROR, va tat propagate de khong dup qua root logger.
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
# Log nguyen payload webhook rat to va co the chua thong tin nhay cam -> mac dinh tat.
# Ban cu log payload bang app.logger.debug() trong khi logger mac dinh o muc INFO
# -> bat LOG_PAYLOAD=1 mot minh KHONG in ra gi ca, phai nho dat them LOG_LEVEL=DEBUG.
# LOG_PAYLOAD la mot lua chon co y cua nguoi van hanh nen log thang o muc INFO.
LOG_PAYLOAD = os.environ.get('LOG_PAYLOAD', '').strip().lower() in ('1', 'true', 'yes')


class _MaxLevelFilter(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return record.levelno < self.level


_log_formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.addFilter(_MaxLevelFilter(logging.ERROR))
_stdout_handler.setFormatter(_log_formatter)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.ERROR)
_stderr_handler.setFormatter(_log_formatter)

app.logger.handlers.clear()
app.logger.addHandler(_stdout_handler)
app.logger.addHandler(_stderr_handler)
app.logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
app.logger.propagate = False

# Gioi han kich thuoc body webhook. Khong co han nay, mot POST khong lo (vo tinh
# hay co y) duoc doc het vao RAM truoc khi parse JSON. Vuot han -> Flask tra 413.
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH_BYTES', 2 * 1024 * 1024))
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# ================== CHONG ALERT TRUNG ==================
# TTLCache tu dong xoa entry sau ALERT_REPEAT_INTERVAL giay - khong bi memory leak
# Lock dam bao thread-safe khi gunicorn dung nhieu threads
ALERT_REPEAT_INTERVAL = int(os.environ.get('ALERT_REPEAT_INTERVAL', 1800))  # giay (mac dinh 30 phut)
ALERT_CACHE_MAXSIZE = int(os.environ.get('ALERT_CACHE_MAXSIZE', 20000))
recent_alerts_cache = TTLCache(maxsize=ALERT_CACHE_MAXSIZE, ttl=ALERT_REPEAT_INTERVAL)
cache_lock = Lock()

_capacity_warn_lock = Lock()
_last_capacity_warn = 0.0
CAPACITY_WARN_INTERVAL = 60  # giay - chong spam log khi cache day

# ================== BACKGROUND SEND QUEUE ==================
# Delay giua 2 message vao cung mot chat de ton trong rate limit cua Telegram
# (20 msg/phut/group).
SEND_DELAY = float(os.environ.get('SEND_DELAY', 3))
# Queue co gioi han: neu Telegram chet lau + alert storm thi queue khong duoc
# phinh vo han den muc OOM. Vuot han -> tra dau dedup va log, de Alertmanager retry.
QUEUE_MAXSIZE = int(os.environ.get('QUEUE_MAXSIZE', 5000))
# Chan so job xu ly trong mot batch, de mot burst khong lam batch qua lon.
MAX_BATCH = int(os.environ.get('MAX_BATCH', 200))

# Ket qua gui: quyet dinh co tha dau dedup ra hay khong.
SEND_OK = 'ok'          # gui thanh cong
SEND_RETRY = 'retry'    # loi tam thoi -> tha dedup de Alertmanager retry di qua
SEND_DROP = 'drop'      # loi vinh vien -> GIU dedup, retry cung se fail y het


class AlertJob(NamedTuple):
    """Mot don vi cong viec trong queue.
    Dung NamedTuple thay tuple tran de khong vo khi unpack luc them field."""
    bot_token: str
    chat_id: str
    message: str
    thread_id: object
    alert_hash: str


alert_queue: "queue.Queue[AlertJob]" = queue.Queue(maxsize=QUEUE_MAXSIZE)

# So job da roi khoi queue nhung CHUA gui xong. Queue rong khong co nghia la da
# gui het - batch cuoi van dang bay. Dem nay de _drain_on_exit() biet cho them.
_inflight = 0
_inflight_lock = Lock()


def _inflight_add(n):
    global _inflight
    with _inflight_lock:
        _inflight += n


def _pending_total():
    with _inflight_lock:
        return alert_queue.qsize() + _inflight

# ================== BOT POOL ==================
# Truoc day moi alert tao mot Bot() moi. Bot cua PTB v21 mo mot HTTPX
# connection pool rieng va khong bao gio duoc shutdown -> ro socket/FD tren
# tien trinh chay dai ngay. Gio cache theo token, tao dung mot lan.
_bots = {}


async def _get_bot(token):
    bot = _bots.get(token)
    if bot is None:
        bot = Bot(token=token)
        await bot.initialize()
        _bots[token] = bot
    return bot


# ================== RATE LIMIT XUYEN BATCH ==================
# Truoc day SEND_DELAY chi ap trong pham vi mot lan drain queue: batch moi den
# ngay sau do se gui tuc thi -> van co the vuot 20 msg/phut. Gio nho moc thoi
# gian gui cuoi cung theo tung chat va cho bu phan con thieu.
_last_sent = {}
_LAST_SENT_MAXSIZE = 2000


async def _throttle(chat_key):
    last = _last_sent.get(chat_key)
    if last is not None:
        wait = SEND_DELAY - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
    _last_sent[chat_key] = time.monotonic()

    if len(_last_sent) > _LAST_SENT_MAXSIZE:
        cutoff = time.monotonic() - max(SEND_DELAY * 10, 60)
        for key in [k for k, v in _last_sent.items() if v < cutoff]:
            _last_sent.pop(key, None)


def _drain_queue():
    """Cho job dau tien (blocking co timeout - khong busy-poll), roi vet not
    nhung job dang cho, toi da MAX_BATCH."""
    items = []
    try:
        items.append(alert_queue.get(timeout=0.5))
    except queue.Empty:
        return items

    try:
        while len(items) < MAX_BATCH:
            items.append(alert_queue.get_nowait())
    except queue.Empty:
        pass

    _inflight_add(len(items))
    return items


async def _send_group(jobs, handled):
    """Gui tuan tu cac job cua cung mot (chat_id, thread_id)."""
    for job in jobs:
        await _throttle((job.chat_id, job.thread_id))
        result = await send_telegram_alert(
            job.bot_token, job.chat_id, job.message, job.thread_id
        )
        handled.add(job.alert_hash)
        if result == SEND_RETRY:
            # Go dau dedup -> lan retry cua Alertmanager di qua duoc,
            # thay vi bi nuot im lang trong ALERT_REPEAT_INTERVAL.
            release_alert(job.alert_hash)


async def _process_batch(items, handled):
    # Group theo (chat_id, thread_id) de moi chat duoc throttle doc lap
    chat_groups = defaultdict(list)
    for job in items:
        chat_groups[(job.chat_id, job.thread_id)].append(job)

    # return_exceptions=True: khong co return_exceptions, mot group nem loi
    # se lam asyncio.gather CANCEL cac group con lai ngay giua chung - nhung
    # chat khong lien quan mat alert oan. Gio moi group song chet doc lap.
    results = await asyncio.gather(
        *[_send_group(g, handled) for g in chat_groups.values()],
        return_exceptions=True,
    )
    for key, result in zip(chat_groups.keys(), results):
        if isinstance(result, BaseException):
            app.logger.error(
                f"Loi khi gui nhom chat {key[0]}: {result}", exc_info=result
            )


def background_worker():
    """Drain alert_queue trong mot thread rieng.
    Khong bao gio de thread chet im lang truoc loi bat ngo."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        items = _drain_queue()
        if not items:
            continue

        handled = set()
        try:
            loop.run_until_complete(_process_batch(items, handled))
        except Exception as e:
            app.logger.error(f"background_worker error: {e}", exc_info=True)
        finally:
            # Chi tha dau dedup cua nhung job CHUA duoc xu ly. Ban cu tha ca
            # batch, ke ca job da gui thanh cong -> alert bi gui trung.
            # Dat trong 'finally' vi tu khi _process_batch nuot exception
            # (return_exceptions=True) thi nhanh 'except' khong con chay nua,
            # ma job bi bo do van phai duoc tha ra.
            for job in items:
                if job.alert_hash not in handled:
                    release_alert(job.alert_hash)
            _inflight_add(-len(items))


# Start background worker thread (daemon=True so it exits with main process)
_worker_thread = Thread(target=background_worker, name='alert-sender', daemon=True)
_worker_thread.start()


# Worker la daemon thread -> bi giet ngay khi tien trinh thoat. Khong co buoc
# nay thi moi lan redeploy / SIGTERM la mat sach nhung alert dang nam trong
# queue. Doi toi da SHUTDOWN_DRAIN_TIMEOUT giay cho worker gui not.
# Dat nho hon graceful_timeout cua gunicorn (30s) de gunicorn khong giet ngang.
SHUTDOWN_DRAIN_TIMEOUT = float(os.environ.get('SHUTDOWN_DRAIN_TIMEOUT', 20))


def _drain_on_exit():
    pending = _pending_total()
    if not pending:
        return
    if not _worker_thread.is_alive():
        # Khong con ai gui thi cho cung vo ich - va se treo tien trinh them
        # SHUTDOWN_DRAIN_TIMEOUT giay truoc khi gunicorn giet ngang.
        app.logger.error(f"Shutdown: worker da chet, mat {pending} alert trong queue")
        return
    app.logger.info(f"Shutdown: cho gui not {pending} alert (toi da {SHUTDOWN_DRAIN_TIMEOUT}s)")
    deadline = time.monotonic() + SHUTDOWN_DRAIN_TIMEOUT
    while time.monotonic() < deadline:
        if not _pending_total():
            app.logger.info("Shutdown: da gui het alert trong queue")
            return
        time.sleep(0.2)
    app.logger.error(f"Shutdown: con {_pending_total()} alert CHUA gui duoc")


atexit.register(_drain_on_exit)
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
    labels = alert.get('labels')
    if not isinstance(labels, dict):
        labels = {}
    return "lb:" + json.dumps(labels, sort_keys=True, default=str)


def build_alert_hash(alert, dest_key=None):
    """Tra ve (alert_hash, key_fields).
    Key gom: identity (full labels) + status + timestamp cua vong doi tuong ung
    -> chan repeat cua Alertmanager, nhung van cho flapping va resolved di qua.

    dest_key = (chat_id, thread_id): dedup tinh RIENG cho tung dich den. Mot
    alert fan-out ra 3 group co 3 dau dedup doc lap, nen khi 1 group gui loi
    va Alertmanager retry, chi group do duoc gui lai - 2 group kia khong bi trung.
    """
    status = str(alert.get('status', '')).lower()
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
        'timestamp': ts,
        'dest': list(dest_key) if dest_key else None,
    }
    alert_hash = md5(json.dumps(key_fields, sort_keys=True, default=str).encode()).hexdigest()
    return alert_hash, key_fields


def _warn_capacity(current):
    """Canh bao cache gan day, toi da 1 lan / CAPACITY_WARN_INTERVAL giay."""
    global _last_capacity_warn
    now = time.monotonic()
    with _capacity_warn_lock:
        if now - _last_capacity_warn < CAPACITY_WARN_INTERVAL:
            return
        _last_capacity_warn = now
    app.logger.warning(
        f"Dedup cache near capacity ({current}/{ALERT_CACHE_MAXSIZE})"
        " - entries may be evicted before TTL, duplicates can slip through"
    )


def reserve_alert(alert, dest_key=None):
    """Tra ve (is_duplicate, alert_hash).
    Danh dau alert ngay khi nhan de chan trung trong cung 1 burst;
    neu gui that bai, background_worker goi release_alert() de tha ra."""
    alert_hash, key_fields = build_alert_hash(alert, dest_key)

    with cache_lock:
        if alert_hash in recent_alerts_cache:
            app.logger.info(
                f"Duplicate alert ({key_fields['status']}) skipped: {key_fields['identity']}"
                + (f" -> chat {dest_key[0]}" if dest_key else "")
            )
            return True, alert_hash

        recent_alerts_cache[alert_hash] = True
        current = len(recent_alerts_cache)

    if current >= ALERT_CACHE_MAXSIZE * 0.9:
        _warn_capacity(current)

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
    """Escape noi dung nam trong code / pre entity cua MarkdownV2.
    Theo spec Telegram: trong code va pre, ca '`' va '\\' deu phai duoc escape.
    Ban cu thay '`' bang nhay don - lam sai lech noi dung alert."""
    return str(text).replace('\\', '\\\\').replace('`', '\\`')


# Chan do dai cua MOT truong ngan (alertname, severity, status, timestamp tho).
# Mot label dai bat thuong khong duoc phep chiem het cho cua ca message: neu
# dong do vuot TELEGRAM_HARD_LIMIT thi _fit_message se bo NGUYEN dong - alert
# mat luon ten - hoac te hon, bo het moi dong va tra ve chuoi rong (Telegram
# tra 400 cho message rong -> alert bi drop han).
MAX_FIELD_CHARS = int(os.environ.get('MAX_FIELD_CHARS', 256))


def _clip(text, limit=None):
    """Cat mot truong ngan ve do dai an toan, TRUOC khi escape."""
    limit = MAX_FIELD_CHARS if limit is None else limit
    text = str(text)
    return text if len(text) <= limit else text[:limit] + '…'


def inline_code(text, limit=None):
    """Mot doan inline code `...` an toan tren MOT dong.

    MarkdownV2 cam ky tu xuong dong ben trong inline code. Mot label co '\\n'
    (alertname, severity... do quy tac Prometheus sinh ra) se lam vo cap
    backtick -> Telegram tra 400 'can't parse entities' -> send_telegram_alert
    coi 400 la loi vinh vien nen alert bi DROP han. Gop moi ky tu xuong dong
    thanh dau cach de noi dung con nguyen ma message van parse duoc.
    """
    one_line = re.sub(r'[\r\n\t]+', ' ', _clip(text, limit))
    return f"`{safe_code(one_line)}`"


def _render_annotation(title, value):
    """Dinh dang mot annotation thanh (cac) dong cua message.

    1 dong:      *Title:* `value`
    Nhieu dong:  *Title:*
                 `dong 1`
                 `dong 2`

    Moi dong duoc boc inline code RIENG. Ly do:
      - Boc ca khoi bang mot cap backtick khong duoc: MarkdownV2 cam xuong dong
        trong inline code -> Telegram tra 400 'can't parse entities'.
      - Dung pre-block ``` cung khong duoc: Telegram ve mot khung lon kem nut
        'copy', doc alert rat roi.
    Boc tung dong cho ra dung mau code nhu Summary ma khong co khung copy.
    """
    text = str(value).strip('\n')
    if '\n' not in text:
        return f"*{title}:* `{safe_code(text)}`"

    lines = [
        f"`{safe_code(line)}`" if line.strip() else ''
        for line in text.split('\n')
    ]
    return f"*{title}:*\n" + '\n'.join(lines)


# ================== DINH DANG TIN NHAN ==================
# Bang cap do theo muc 5.3 cua quy dinh giam sat. Ban cu chi biet critical va
# warning nen major va minor roi het vao nhanh else -> hien thi icon "info",
# sai han muc do nghiem trong.
# Icon chon theo NGU NGHIA chu khong theo mau: doc duoc ca tren man hinh den
# trang va voi nguoi mu mau do/cam - mau khong phai kenh thong tin duy nhat.
# Chi map severity -> icon. Ten tieng Viet (NGHIEM TRONG, CANH BAO LON...) da
# duoc go: dong Severity in dung gia tri goc cua label, de khop voi ten severity
# trong alert rule / Alertmanager va de grep.
SEVERITY_ICONS = {
    'critical': '⛔',
    'major':    '❗',
    'minor':    '⚠️',
    'warning':  '🔹',
    'info':     'ℹ️',
}

# Severity khong co trong SEVERITY_ICONS (vd 'page', 'none', hoac rong). KHONG
# duoc lay icon cua muc 'info' lam mac dinh, khong thi mot alert dang firing voi
# severity la se nhin y het mot ghi nhan vo thuong vo phat. Firing -> coi nhu
# nghiem trong; resolved -> danh dau '?' vi cap do khong xac dinh.
SEVERITY_ICON_UNKNOWN_FIRING = '⛔'
SEVERITY_ICON_UNKNOWN_RESOLVED = '❔'

# Status khong phai firing / resolved (rong, 'suppressed', sai chinh ta...).
# Khong duoc gop vao nhanh firing: bao "DANG CANH BAO" cho mot payload di dang
# la noi doi voi nguoi truc. In nguyen trang thai de biet payload bat thuong.
STATUS_ICON_UNKNOWN = '❔'

# endsAt cua alert dang firing la zero time cua Go. Moi phep tinh thoi luong
# deu phai chan gia tri nay, khong thi ra so am khong lo.
_ZERO_TS_PREFIX = '0001-01-01'

# Hau to gan vao tieu de Summary/Description trong tin RESOLVED. Noi dung hai
# truong do la anh chup luc canh bao, khong phai trang thai hien tai.
# Dat '' de bo hoan toan.
ANNOTATION_SUFFIX_RESOLVED = ' (firing)'

# Telegram cat cung o 4096 ky tu cho sendMessage. Vuot han -> BadRequest, ma
# send_telegram_alert coi BadRequest la loi VINH VIEN nen alert bi drop va dau
# dedup duoc giu nguyen -> canh bao bien mat hoan toan trong ALERT_REPEAT_INTERVAL.
# Description cua alert Ceph/Loki rat de vuot 4096. Chua bien an toan cho phan
# escape cua MarkdownV2 (moi ky tu dac biet ton them 1 ky tu).
TELEGRAM_HARD_LIMIT = 4096
MAX_MESSAGE_CHARS = min(
    int(os.environ.get('MAX_MESSAGE_CHARS', 3800)), TELEGRAM_HARD_LIMIT
)
# Nam BEN TRONG inline code nen khong can escape MarkdownV2 - trong code entity
# chi '`' va '\' moi phai escape.
TRUNCATE_MARK = ' …(đã cắt bớt)'
# Ngan sach toi thieu de danh cho MOI annotation con lai khi mot annotation
# truoc no phai cat. Khong co no thi Summary dai bat thuong nuot sach Description.
MIN_ANNOTATION_CHARS = int(os.environ.get('MIN_ANNOTATION_CHARS', 120))
# Nam ngoai code entity -> phai escape.
TRUNCATED_LINE = escape_md2('… (nội dung quá dài, đã lược bớt)')


def _parse_ts(raw):
    """Parse timestamp cua Alertmanager, tra ve None neu rong / zero / hong."""
    if not raw or str(raw).startswith(_ZERO_TS_PREFIX):
        return None
    try:
        return parser.parse(raw)
    except Exception as e:
        app.logger.warning(f"Failed to parse timestamp '{raw}': {e}")
        return None


# Mui gio hien thi. Truoc day hardcode 'Asia/Bangkok' - cung offset +07 nen
# gio hien ra van dung, nhung ten mui gio sai voi he thong dat tai Viet Nam.
DISPLAY_TZ_NAME = os.environ.get('DISPLAY_TZ', 'Asia/Ho_Chi_Minh')
try:
    DISPLAY_TZ = pytz.timezone(DISPLAY_TZ_NAME)
except pytz.UnknownTimeZoneError:
    app.logger.error(
        f"DISPLAY_TZ='{DISPLAY_TZ_NAME}' khong hop le - dung Asia/Ho_Chi_Minh"
    )
    DISPLAY_TZ_NAME = 'Asia/Ho_Chi_Minh'
    DISPLAY_TZ = pytz.timezone(DISPLAY_TZ_NAME)


def _fmt_ts(dt):
    return dt.astimezone(DISPLAY_TZ).strftime('%Y-%m-%d %H:%M:%S')


def _ts_line(title, dt, raw):
    """Mot dong thoi gian, hoac None neu khong co gi de in.

    Parse duoc -> in gio Viet Nam. Parse KHONG duoc nhung payload van co gia
    tri tho -> in nguyen ban: mat dinh dang con hon mat han moc thoi gian,
    va nguoi truc nhin ra ngay la Alertmanager gui timestamp la.
    `title` khong duoc chua ky tu dac biet cua MarkdownV2.
    """
    if dt is not None:
        return f"*{title}:* {inline_code(_fmt_ts(dt))}"
    if raw and not str(raw).startswith(_ZERO_TS_PREFIX):
        return f"*{title}:* {inline_code(raw)}"
    return None


def _fit_annotation(title_md, text, budget):
    """Doan dai nhat cua `text` ma khi render van lot `budget`, hoac None.

    Tim NHI PHAN thay vi rut 25% moi vong nhu ban cu: ban cu co the bo phi toi
    mot phan tu ngan sach (vd chot o 2700 ky tu trong khi con cho toi 3600),
    keo theo viec annotation phia sau bi bo han du van con cho.
    Do dai sau escape tang don dieu theo do dai tho nen nhi phan dung; va vi
    moi ung vien deu duoc do lai truoc khi nhan, ket qua khong bao gio vuot han.
    """
    lo, hi, best = 1, min(len(text), budget), None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _render_annotation(title_md, text[:mid] + TRUNCATE_MARK)
        if len(candidate) <= budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _append_annotations(lines, annotations, suffix, budget):
    """Them cac dong annotation vao `lines`, khong vuot `budget` ky tu.

    Cat theo GIA TRI THO roi moi escape: cat sau khi escape co the dut doi mot
    cap '\\x' hoac mot cap backtick -> Telegram tra 400 va alert bi drop han.
    Cat truoc khi escape thi ket qua luon con la MarkdownV2 hop le.

    Mot annotation phai cat KHONG lam dung viec xet nhung annotation sau no:
    ban cu 'break' ngay, nen mot Summary dai bat thuong nuot sach Description -
    truong thuong chua huong dan xu ly.
    """
    fields = [(f, t) for f, t in (('info', 'Info'), ('summary', 'Summary'),
                                  ('description', 'Description'))
              if annotations.get(f) not in (None, '')]

    truncated = False
    for idx, (field, title) in enumerate(fields):
        if budget <= 0:
            truncated = True
            break

        title_md = escape_md2(title + suffix)
        text = str(annotations[field])

        # Escape chi lam chuoi DAI THEM, khong bao gio ngan di. Nen mot gia tri
        # tho da dai hon ngan sach thi chac chan khong vua - khong can escape
        # ca chuoi (co the vai MB) chi de do do dai roi vut di.
        if len(text) <= budget:
            rendered = _render_annotation(title_md, text)
            if len(rendered) <= budget:
                lines.append(rendered)
                budget -= len(rendered) + 1  # +1 cho ky tu xuong dong
                continue

        # Khong vua: chua lai mot phan ngan sach cho cac annotation phia sau.
        # Neu chua lai roi ma chinh no khong con cho, thi dung het phan con lai.
        reserve = MIN_ANNOTATION_CHARS * (len(fields) - idx - 1)
        rendered = _fit_annotation(title_md, text, max(budget - reserve, 0))
        if rendered is None:
            rendered = _fit_annotation(title_md, text, budget)
        if rendered is not None:
            lines.append(rendered)
            budget -= len(rendered) + 1
        truncated = True

    if truncated:
        lines.append(TRUNCATED_LINE)


def _fit_message(lines):
    """Chot chan cuoi cung: ghep `lines` sao cho khong vuot TELEGRAM_HARD_LIMIT.

    Chi cat o RANH GIOI DONG. Moi dong do format_telegram_message sinh ra deu
    tu dong (mo va dong day du cac entity MarkdownV2), nen cat theo dong khong
    bao gio de lai entity ho.
    """
    out = []
    total = 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if total + extra > TELEGRAM_HARD_LIMIT:
            break
        out.append(line)
        total += extra

    if not out:
        # Khong dong nao lot - khong bao gio duoc tra ve chuoi rong: Telegram
        # tra 400 cho message rong, ma 400 bi coi la loi vinh vien nen alert
        # se bi drop im lang. Voi _clip o cac truong ngan thi nhanh nay gan
        # nhu khong xay ra, nhung day la chot chan cuoi cung.
        app.logger.error("Message vuot gioi han ngay tu dong dau - gui ban rut gon")
        return TRUNCATED_LINE

    return '\n'.join(out)


def format_telegram_message(alert, labels, annotations):
    """Dung noi dung tin nhan Telegram.

    Tin FIRING : Status, Alertname, Severity, Summary, Description, Bat dau.
    Tin RESOLVED: nhu tren nhung tieu de Summary/Description co them
                 "(firing)", va moc thoi gian la Ket thuc thay cho Bat dau.
    Status khac : in nguyen trang thai kem icon '?', khong gia vo la firing.

    NGOAI severity, KHONG label nao khac duoc dua vao tin nhan (truoc day co
    them dong "May chu / Site"): chi tiet may chu, site, osd... da nam trong
    Description do alert rule sinh ra, in lai o tren chi lam tin dai gap doi.

    Do dai luon duoc chan duoi TELEGRAM_HARD_LIMIT - xem _append_annotations
    va _fit_message.

    Summary/Description trong payload resolved la anh chup luc alert dang
    firing chu khong phai trang thai hien tai - do la ly do co hau to.
    """
    status = str(alert.get('status', '')).lower()
    # _clip: severity cung den tu label ben ngoai. Mot severity dai bat thuong
    # khong duoc phep an het ngan sach ky tu cua phan Summary/Description.
    severity = _clip(labels.get('severity', '')).lower()
    is_resolved = status == 'resolved'
    is_firing = status == 'firing'

    sev_icon = SEVERITY_ICONS.get(severity, '')
    if not sev_icon:
        sev_icon = (SEVERITY_ICON_UNKNOWN_RESOLVED if is_resolved
                    else SEVERITY_ICON_UNKNOWN_FIRING)

    if is_resolved:
        status_icon = '✅'
        status_text = 'RESOLVED'
    elif is_firing:
        status_icon = sev_icon
        status_text = 'FIRING'
    else:
        status_icon = STATUS_ICON_UNKNOWN
        # _clip: status la mot chuoi tu payload ben ngoai, khong co gi bao dam
        # no ngan. Khong chan thi mot status dai bat thuong day ca dong Status
        # vuot gioi han -> _fit_message bo dong do -> message RONG -> 400.
        status_text = _clip(alert.get('status', '')).upper() or 'UNKNOWN'

    lines = [
        f"*Status:* {status_icon} {escape_md2(status_text)} {status_icon}",
        f"*Alertname:* {inline_code(labels.get('alertname', 'N/A'))}",
    ]

    # Severity: luon hien thi, ke ca khi da khoi phuc - nguoi doc can biet
    # su co vua roi nghiem trong den muc nao. In gia tri goc cua label
    # (critical / major / ...), khong dich sang tieng Viet.
    if severity:
        lines.append(f"*Severity:* {sev_icon} {escape_md2(severity)}")

    # Tieu de PHAI escape truoc khi dua vao _render_annotation: ham do noi
    # thang vao "*{title}:*", ma hau to "(luc canh bao)" co dau ngoac - ky tu
    # dac biet cua MarkdownV2. Khong escape thi Telegram tra 400
    # "can't parse entities", va send_telegram_alert coi 400 la loi vinh vien
    # nen alert bi drop han, khong retry.
    suffix = ANNOTATION_SUFFIX_RESOLVED if is_resolved else ''

    # Dung phan duoi (thoi gian) TRUOC de biet con bao nhieu cho cho annotation.
    raw_start = alert.get('startsAt')
    raw_end = alert.get('endsAt')
    started = _parse_ts(raw_start)
    ended = _parse_ts(raw_end)

    tail = []
    if is_resolved:
        # Tin resolved chi in "Ket thuc". Moc bat dau da co trong tin firing
        # gui truoc do, lap lai o day chi lam tin dai them.
        end_line = _ts_line('Kết thúc', ended, raw_end)
        if end_line:
            tail.append(end_line)
    else:
        start_line = _ts_line('Bắt đầu', started, raw_start)
        if start_line:
            tail.append(start_line)

    head_len = sum(len(x) + 1 for x in lines)
    tail_len = sum(len(x) + 1 for x in tail)
    # Tru them do dai TRUNCATED_LINE de con cho bao "da luoc bot" neu phai cat.
    budget = MAX_MESSAGE_CHARS - head_len - tail_len - len(TRUNCATED_LINE) - 1
    _append_annotations(lines, annotations, suffix, budget)

    lines.extend(tail)
    return _fit_message(lines)


async def send_telegram_alert(bot_token, chat_id, message, thread_id=None, max_retries=3):
    """Gui alert den Telegram. Tra ve SEND_OK / SEND_RETRY / SEND_DROP.

    Phan biet loi tam thoi (mang, flood control) voi loi vinh vien (400 sai
    markup, 403 bot bi kick, token sai). Ban cu coi tat ca la that bai roi tha
    dau dedup -> Alertmanager retry -> lai fail y het -> vong lap vo han.
    """
    if not bot_token or not chat_id:
        app.logger.error("bot_token or chat_id is None - Skip sending message")
        return SEND_DROP

    # Validate thread_id truoc khi vao vong retry de khong nem ValueError
    thread_id_int = None
    if thread_id is not None:
        try:
            thread_id_int = int(thread_id)
        except (ValueError, TypeError) as e:
            app.logger.error(f"Invalid MESSAGE_THREAD_ID '{thread_id}': {e} - sending without thread")

    try:
        bot = await _get_bot(bot_token)
    except InvalidToken as e:
        app.logger.error(f"Invalid bot token: {e} - drop alert, khong retry")
        return SEND_DROP
    except Exception as e:
        app.logger.error(f"Cannot init bot: {e}")
        return SEND_RETRY

    send_kwargs = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': ParseMode.MARKDOWN_V2,
    }
    if thread_id_int is not None:
        send_kwargs['message_thread_id'] = thread_id_int

    for attempt in range(1, max_retries + 1):
        try:
            await bot.send_message(**send_kwargs)
            app.logger.info(f"Sent alert to chat_id {chat_id} success")
            return SEND_OK

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

        except ChatMigrated as e:
            app.logger.error(
                f"chat_id {chat_id} da migrate sang {e.new_chat_id} - "
                "cap nhat CHAT_ID trong config. Drop alert, khong retry"
            )
            return SEND_DROP

        except (BadRequest, Forbidden, InvalidToken) as e:
            # 400 (markup/chat sai), 403 (bot bi kick / chua duoc add vao group),
            # token sai: retry bao nhieu lan cung fail y het.
            app.logger.error(
                f"Permanent Telegram error for chat_id {chat_id}: {e} - drop alert, khong retry"
            )
            return SEND_DROP

        except TelegramError as e:
            app.logger.warning(
                f"Telegram error: {e}. Retry in 3s (attempt {attempt}/{max_retries})"
            )
            if attempt < max_retries:
                await asyncio.sleep(3)

        except Exception as e:
            app.logger.error(f"Failed to send alert: {e}", exc_info=True)
            return SEND_RETRY

    app.logger.error(f"Failed to send alert to chat_id {chat_id} after {max_retries} attempts")
    return SEND_RETRY


def resolve_destinations(labels):
    """Danh sach (bot_token, chat_id, thread_id) ma alert nay phai duoc gui den.

    Mot alert co the ra nhieu group: rule khai bao nhieu 'targets', va/hoac
    nhieu rule cung khop nho "continue": true. Dich trung nhau (cung chat +
    topic) chi giu mot lan de khong gui doi.
    """
    rules = find_rules(labels)
    if not rules:
        app.logger.warning(f"No rule matched for labels: {labels}. Use DEFAULT bot")
        return [(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, DEFAULT_MESSAGE_THREAD_ID)]

    destinations = []
    seen = set()
    for rule in rules:
        for target in rule.targets:
            thread_id = target.thread_id if target.thread_id is not None else DEFAULT_MESSAGE_THREAD_ID
            key = (target.chat_id, thread_id)
            if key in seen:
                continue
            seen.add(key)
            destinations.append((target.bot_token, target.chat_id, thread_id))

    app.logger.info(
        f"Rule matched: {', '.join(r.name for r in rules)}"
        f" -> {len(destinations)} dich den"
    )
    return destinations


@app.route('/health', methods=['GET'])
def health():
    """Cho HEALTHCHECK cua Docker / liveness probe. Khong can auth."""
    alive = _worker_thread.is_alive()
    # TTLCache KHONG thread-safe: len() goi expire() - tuc la GHI vao cache.
    # Doc khong lock trong khi worker/webhook dang ghi co the ra
    # "RuntimeError: dictionary changed size during iteration".
    with cache_lock:
        cache_size = len(recent_alerts_cache)
    body = {
        'status': 'ok' if alive else 'degraded',
        'worker_alive': alive,
        'queue_size': alert_queue.qsize(),
        'queue_maxsize': QUEUE_MAXSIZE,
        'dedup_cache_size': cache_size,
        'rules_loaded': len(RULES),
        'default_bot_configured': bool(DEFAULT_BOT_TOKEN and DEFAULT_CHAT_ID),
    }
    return jsonify(body), 200 if alive else 503


@app.route('/alert', methods=['POST'])
@auth.login_required
def alertmanager_webhook():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        app.logger.error("Invalid request - body khong phai JSON object")
        return jsonify({'status': 'error', 'message': 'Invalid request'}), 400

    alerts = data.get('alerts')
    if not isinstance(alerts, list):
        app.logger.error("Invalid request - 'alerts' thieu hoac khong phai mang")
        return jsonify({'status': 'error', 'message': 'Invalid request'}), 400

    app.logger.info("Webhook called")
    if LOG_PAYLOAD:
        # Log o INFO chu khong DEBUG: xem ghi chu o dinh nghia LOG_PAYLOAD.
        app.logger.info(f"Received data: {json.dumps(data, default=str)}")

    # Enqueue alerts cho background worker -> tra 200 ngay.
    # background_worker() drain queue voi rate limit (SEND_DELAY giua cac msg),
    # tranh gunicorn timeout va Alertmanager retry tren batch cham.
    queued = 0
    duplicates = 0
    dropped = 0     # loi VINH VIEN (payload di dang) - Alertmanager retry cung the
    rejected = 0    # loi TAM THOI (queue day) - phai bao 5xx de duoc retry

    for alert in alerts:
        if not isinstance(alert, dict):
            app.logger.warning(f"Bo qua phan tu 'alerts' khong phai object: {type(alert).__name__}")
            dropped += 1
            continue

        labels = alert.get('labels')
        if not isinstance(labels, dict):
            labels = {}
        annotations = alert.get('annotations')
        if not isinstance(annotations, dict):
            annotations = {}

        # Mot alert di dang / mot rule cau hinh sai khong duoc lam hong ca batch.
        # Ban cu de exception thoat ra 500 (vd TypeError khi entry trong
        # telegram_config.json la mang [...] thay vi object {...}), keo theo
        # nhung alert da reserve nhung chua vao queue bi nuot mat ca chu ky dedup.
        try:
            destinations = resolve_destinations(labels)
            message = format_telegram_message(alert, labels, annotations)
        except Exception as e:
            app.logger.error(f"Khong xu ly duoc alert {labels}: {e}", exc_info=True)
            dropped += 1
            continue

        # Dedup tinh rieng cho tung dich: mot group gui loi khong keo theo
        # viec gui lai o nhung group da nhan duoc.
        for bot_token, chat_id, thread_id in destinations:
            # reserve_alert() da log chi tiet khi trung -> khong log lap o day
            is_dup, alert_hash = reserve_alert(alert, (chat_id, thread_id))
            if is_dup:
                duplicates += 1
                continue

            try:
                alert_queue.put_nowait(
                    AlertJob(bot_token, chat_id, message, thread_id, alert_hash)
                )
            except queue.Full:
                app.logger.error(
                    f"Queue day ({QUEUE_MAXSIZE}) - tu choi alert {labels.get('alertname')} "
                    f"-> chat {chat_id}. Telegram co the dang khong gui duoc; "
                    "tra 503 de Alertmanager retry"
                )
                release_alert(alert_hash)
                rejected += 1
                continue

            queued += 1

    app.logger.info(
        f"Queued {queued} alert(s), {duplicates} duplicate(s), "
        f"{dropped} dropped, {rejected} rejected"
    )

    body = {
        'status': 'ok' if not rejected else 'overloaded',
        'queued': queued,
        'duplicates': duplicates,
        'dropped': dropped,
        'rejected': rejected,
    }

    # Alertmanager CHI retry khi nhan 5xx. Ban cu tra 200 ke ca khi queue day
    # -> alert bi tu choi im lang, phai doi het repeat_interval (thuong 4h)
    # moi co co hoi gui lai. Phan biet ro hai loai that bai:
    #   - rejected (queue day)  : loi tam thoi -> 503, retry co ich.
    #   - dropped  (payload hong): loi vinh vien -> 200, retry chi lam lap vo han.
    if rejected:
        return jsonify(body), 503
    return jsonify(body), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9119)
