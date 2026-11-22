"""Daily agent run: fetch -> store -> match each profile -> send only NEW matches to Telegram.

CLI:
  python -m tools.run_daily                                  # all profiles in profiles/
  python -m tools.run_daily --profile women_contractor_kiambu --dry-run   # print, don't send
"""

import argparse
from pathlib import Path

from tools import digest, fetch_tenders, store, telegram
from tools.match import load_profile, match

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


def run(profile_ids=None, dry_run=False, top=8):
    db = store.connect()
    tenders = fetch_tenders.fetch_active()
    new = store.upsert_tenders(db, tenders)
    print(f"Fetched {len(tenders)} active tenders ({len(new)} new).")

    paths = sorted(PROFILES_DIR.glob("*.yaml"))
    if profile_ids:
        paths = [p for p in paths if p.stem in profile_ids]
    for path in paths:
        profile = load_profile(path)
        results = match(store.open_tenders(db), profile, db=db, top=top * 2)
        sent = store.already_sent(db, profile["id"])
        fresh = [m for m in results if m["id"] not in sent][:top]
        messages = digest.build_messages(profile["name"], fresh, new_only=True)
        print(f"\n[{profile['id']}] {len(results)} matches, {len(fresh)} not sent before.")
        if dry_run or not telegram.configured():
            if not dry_run:
                print("(Telegram not configured: printing instead)")
            for msg in messages:
                print(msg, "\n")
            continue
        for msg in messages:
            telegram.send(msg)
        store.mark_sent(db, profile["id"], [m["id"] for m in fresh])
        print(f"Sent {len(messages)} message(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="append", help="profile file stem; repeatable (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()
    run(a.profile, a.dry_run, a.top)


if __name__ == "__main__":
    main()
