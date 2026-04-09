#!/usr/bin/env python3
"""Send notifications via Slack. Reads SLACK_BOT_TOKEN and SLACK_CHANNEL_ID from env."""

import os
import sys
import json
import urllib.request

def send(message):
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    if not token or not channel:
        print(f"[notify] {message}")
        return

    data = json.dumps({"channel": channel, "text": message}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"[notify] Slack failed: {e}. Message: {message}")

if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "ping"
    send(msg)
