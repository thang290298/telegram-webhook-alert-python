import os
import json

with open('telegram_config.json', 'r') as file:
    CONFIG_DATA = json.load(file)

PRIORITY_LABELS = CONFIG_DATA.get('PRIORITY_LABELS', [])

TELEGRAM_CONFIG = {key: value for key, value in CONFIG_DATA.items() if key != 'PRIORITY_LABELS'}

DEFAULT_BOT_TOKEN = os.environ.get('DEFAULT_BOT_TOKEN')
DEFAULT_CHAT_ID = os.environ.get('DEFAULT_CHAT_ID')
DEFAULT_MESSAGE_THREAD_ID = os.environ.get('DEFAULT_MESSAGE_THREAD_ID')