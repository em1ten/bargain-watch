#!/usr/bin/env python3
"""
Sends one summary ntfy notification covering everything queued up by
"digest"-mode watches since the last digest, then clears the queue.

Meant to run once a day via .github/workflows/digest.yml — keeps
lower-priority brands from pinging your phone every 20 minutes while
still surfacing what turned up.
"""

import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DIGEST_PATH = ROOT / "digest_pending.json"


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not DIGEST_PATH.exists():
        print("No digest file yet, nothing to send.")
        return

    with open(DIGEST_PATH) as f:
        pending = json.load(f)

    total = sum(len(items) for items in pending.values())
    if total == 0:
        print("Digest is empty, nothing to send.")
        return

    if not topic:
        print(f"{total} items queued but NTFY_TOPIC isn't set — skipping push.")
    else:
        lines = []
        for watch_name, items in pending.items():
            if not items:
                continue
            lines.append(f"{watch_name} ({len(items)}):")
            for c in items[:4]:
                lines.append(f"  {c['price']}: {c['title']}")
            if len(items) > 4:
                lines.append(f"  …and {len(items) - 4} more")
        message = "\n".join(lines)

        try:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=message.encode("utf-8"),
                headers={
                    "Title": f"Vinted digest: {total} new items".encode("utf-8"),
                    "Priority": "default",
                    "Tags": "shirt,calendar",
                },
                timeout=15,
            )
            print(f"Digest sent: {total} items.")
        except requests.RequestException as e:
            print(f"! ntfy push failed: {e}")
            return  # don't clear the queue if the push failed

    # Clear the queue now that it's been sent (or intentionally skipped with no topic set)
    with open(DIGEST_PATH, "w") as f:
        json.dump({}, f)


if __name__ == "__main__":
    main()
