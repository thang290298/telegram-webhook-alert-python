#!/usr/bin/env python3
"""Kiem tra telegram_config.json truoc khi deploy - khong can Flask/Telegram.

    python tools/check_config.py [duong_dan_config]
    python tools/check_config.py telegram_config.json site=dbp3 service=openstack

Khong co cap label -> chi liet ke rule da nap theo thu tu uu tien.
Co cap label     -> mo phong xem alert do se duoc gui den chat nao.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    args = sys.argv[1:]
    path = None
    labels = {}

    for arg in args:
        if '=' in arg:
            key, _, value = arg.partition('=')
            labels[key] = value
        elif path is None:
            path = arg
        else:
            print(f"Tham so khong hieu: {arg}", file=sys.stderr)
            return 2

    if path:
        os.environ['TELEGRAM_CONFIG_PATH'] = path

    print("=" * 62)
    try:
        import config
    except Exception as e:
        print(f"\nCONFIG HONG: {type(e).__name__}: {e}")
        print("Container se KHONG khoi dong duoc voi file nay.")
        return 1
    print("=" * 62)

    # Dem so entry dang le phai thanh rule, de phat hien entry bi loai am tham
    raw = config.CONFIG_DATA
    candidates = set()
    nested = raw.get('rules')
    if isinstance(nested, dict):
        candidates |= {k for k in nested if not k.startswith('_')}
    candidates |= {
        k for k in raw
        if k not in ('PRIORITY_LABELS', 'rules') and not k.startswith('_')
    }
    rejected = len(candidates) - len(config.RULES)

    print(f"\nFile           : {config.CONFIG_PATH}")
    print(f"PRIORITY_LABELS: {config.PRIORITY_LABELS or '(rong)'}")
    print(f"So rule hop le : {len(config.RULES)}/{len(candidates)}\n")

    if not config.RULES:
        print("  (khong co rule nao - moi alert se ve DEFAULT bot)")
    else:
        print(f"  {'#':<3} {'RULE':<20} {'DIEU KIEN':<38} {'CHAT_ID':<17} THREAD")
        print(f"  {'-'*3} {'-'*20} {'-'*38} {'-'*17} ------")
        for i, rule in enumerate(config.RULES, 1):
            cond = ', '.join(
                f"{k}={v}" if k else f"*={v}" for k, v in rule.matchers
            )
            if rule.alt_value:
                cond += f" | *={rule.alt_value}"
            if len(cond) > 38:
                cond = cond[:35] + '...'
            name = rule.name + (' [continue]' if rule.cont else '')
            first = rule.targets[0]
            print(f"  {i:<3} {name:<20} {cond:<38} {first.chat_id:<17} "
                  f"{first.thread_id or '-'}")
            for extra in rule.targets[1:]:
                print(f"  {'':<3} {'  + them dich':<20} {'':<38} "
                      f"{extra.chat_id:<17} {extra.thread_id or '-'}")
        if any(not r.explicit for r in config.RULES):
            print("\n  '*' = bat ky label nao trong PRIORITY_LABELS (rule dang cu)")

    if rejected > 0:
        bad = sorted(candidates - {r.name for r in config.RULES})
        print(f"\n  !! {rejected} entry BI LOAI: {', '.join(bad)}")
        print("     Xem dong '[ERROR] config:' o tren de biet ly do.")
        print("     Alert khop nhung key nay se roi ve DEFAULT bot, KHONG dung group.")

    if not labels:
        print("\nMo phong routing: them cap label vao lenh, vi du")
        print("  python tools/check_config.py telegram_config.json site=dbp3 service=openstack")
        return 1 if rejected else 0

    print(f"\n{'=' * 62}")
    print(f"Alert co labels: {labels}")
    rules = config.find_rules(labels)
    if not rules:
        print("-> KHONG rule nao khop. Gui bang DEFAULT_BOT_TOKEN / DEFAULT_CHAT_ID.")
        if config.PRIORITY_LABELS:
            ngoai = [k for k in labels if k not in config.PRIORITY_LABELS]
            if ngoai:
                print(f"   Luu y: {ngoai} khong nam trong PRIORITY_LABELS nen bi bo qua khi match.")
    else:
        print(f"-> Khop rule: {', '.join(r.name for r in rules)}")
        seen, n = set(), 0
        for rule in rules:
            for t in rule.targets:
                thread = t.thread_id or config.DEFAULT_MESSAGE_THREAD_ID
                if (t.chat_id, thread) in seen:
                    continue
                seen.add((t.chat_id, thread))
                n += 1
                print(f"   [{n}] rule '{rule.name}'"
                      f"  CHAT_ID={t.chat_id}"
                      f"  THREAD={thread or '-'}"
                      f"  BOT={t.bot_token[:10]}...")
        print(f"   => alert nay se duoc gui den {n} noi")
    print("=" * 62)
    return 1 if rejected else 0


if __name__ == '__main__':
    sys.exit(main())
