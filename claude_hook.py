#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code -> Telegram bildirimi (kanca / hook).

Claude Code bir işi bitirince ("Stop") ya da senden izin/onay isteyince ("Notification")
bu betik çalışır ve Telegram'a kısa bir mesaj gönderir. Köprünün (bridge.py) çalışıyor
olması GEREKMEZ; sadece aynı klasördeki ayarlar.env (bot token + kullanıcı no) yeterli.

Kurulum: README.md içindeki "Claude Code" bölümüne bak.
"""
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge  # noqa: E402


def last_assistant_text(transcript, limit=700):
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines[-400:]):
        try:
            o = json.loads(line)
        except Exception:
            continue
        msg = o.get("message") if isinstance(o, dict) else None
        if not msg or msg.get("role") != "assistant":
            continue
        c = msg.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = (c or "").strip()
        if c:
            return c if len(c) <= limit else c[:limit] + "…"
    return ""


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    cfg = bridge.load_env()
    if not cfg["TELEGRAM_BOT_TOKEN"] or not cfg["TELEGRAM_USER_ID"]:
        return 0
    ev = data.get("hook_event_name", "")
    project = Path(data.get("cwd") or ".").name or "(bilinmiyor)"
    hh = time.strftime("%H:%M")
    if ev == "Stop":
        if data.get("stop_hook_active"):
            return 0
        text = "✅ Claude Code işlemi bitirdi\n\n📁 Proje: %s  •  🕐 %s" % (project, hh)
        tail = last_assistant_text(data.get("transcript_path", ""))
        if tail:
            text += "\n\n💬 " + tail
    elif ev == "Notification":
        text = "⏸ Claude Code senden bir şey bekliyor\n\n📁 Proje: %s  •  🕐 %s\n\n%s" % (
            project, hh, data.get("message", ""))
    else:
        return 0
    try:
        bridge.Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_API_BASE"]).send_text(cfg["TELEGRAM_USER_ID"], text)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
