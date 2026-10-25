#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Yedek yol: Telegram'a elle mesaj/dosya yollar.
Kullanım:
  python telegram_gonder.py "Merhaba"
  python telegram_gonder.py --dosya C:\\yol\\rapor.md
Antigravity'ye "iş bitince bu komutu çalıştır" dersen köprüden bağımsız bildirim de alırsın (isteğe bağlı)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge

def main():
    cfg = bridge.load_env()
    if not cfg["TELEGRAM_BOT_TOKEN"] or not cfg["TELEGRAM_USER_ID"]:
        print("Önce Kurulum.bat çalıştırılmalı ve Telegram eşleşmesi yapılmalı."); return 1
    tg = bridge.Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_API_BASE"])
    a = sys.argv[1:]
    if a and a[0] == "--dosya" and len(a) > 1:
        p = Path(a[1]); tg.send_document(cfg["TELEGRAM_USER_ID"], p.name, p.read_bytes())
    else:
        tg.send_text(cfg["TELEGRAM_USER_ID"], " ".join(a) if a else sys.stdin.read())
    return 0

if __name__ == "__main__":
    sys.exit(main())
