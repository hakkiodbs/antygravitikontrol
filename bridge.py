#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity <-> Telegram köprüsü

Ne yapar?
  1) Antigravity'deki TÜM konuşmaları (projeler) izler. Bir iş bitince son cevabı Telegram'a yollar.
  2) Telegram'dan yazdığın mesajı Antigravity sohbetine yazıp gönderir.

Antigravity'nin arayüzüne Chrome DevTools Protocol (CDP) ile bağlanır.
Bunun için Antigravity "--remote-debugging-port=9222" ile açılmış olmalı
(Kurulum bunun için masaüstüne özel kısayol koyar).
"""
import os
import sys
import re
import json
import time
import base64
import random
import socket
import hashlib
import threading
import traceback
import subprocess
import argparse
import uuid
import shutil
import mimetypes
import mimetypes
import base64
import urllib.request
import urllib.error
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / "ayarlar.env"
LOG_FILE = BASE / "kopru.log"
LOCK_PORT = 47653
VERSION = "8.8"

DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_USER_ID": "",
    "CDP_PORT": "9222",
    "POLL_SECONDS": "3",
    "DONE_CONFIRM": "4",
    "UI_MARKERS": "Conversation History,Customizations,Ask anything,New Conversation",
    "TELEGRAM_API_BASE": "https://api.telegram.org",
    "ANTIGRAVITY_EXE": "",
    "AUTO_ANSWER": "0",
    "HEADER_CHECK": "1",
    "OPENAI_API_KEY": "",
    "GELEN_KLASOR": "",
    "ATTACH_MODE": "auto",
}


# --------------------------------------------------------------------------- ayarlar / log
def load_env():
    cfg = dict(DEFAULTS)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in DEFAULTS:  # ortam değişkeni varsa onu da kabul et
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def save_env_value(key, value):
    lines = []
    found = False
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = "%s=%s" % (key, value)
            found = True
    if not found:
        lines.append("%s=%s" % (key, value))
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass



# --------------------------------------------------------------------------- Gizli bilgi maskeleme, git özeti
SECRET_PATTERNS = [
    (re.compile(r"\b(sk-[a-zA-Z0-9_\-]{20,})\b"), "[GİZLİ_ANAHTAR]"),
    (re.compile(r"\b(AIzaSy[a-zA-Z0-9_\-]{33})\b"), "[GİZLİ_GOOGLE_ANAHTARI]"),
    (re.compile(r"\b(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{50,})\b"), "[GİZLİ_GITHUB_TOKEN]"),
    (re.compile(r"\b(xox[baprs]-[a-zA-Z0-9_\-]{10,})\b"), "[GİZLİ_SLACK_TOKEN]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), "[GİZLİ_TELEGRAM_TOKEN]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[GİZLİ_ÖZEL_ANAHTAR]"),
    (re.compile(r"(?i)\b(bearer\s+)[a-zA-Z0-9_\-\.]{25,}\b"), r"\1[GİZLİ_BEARER]"),
    (re.compile(r"(https?://[^:\s/]+:)([^@\s]+)(@)"), r"\1***\3"),
]


def redact_secrets(text):
    """Telegram'a giden metindeki API anahtarı, token ve şifreleri maskeler."""
    if not text:
        return text
    for rx, rep_ in SECRET_PATTERNS:
        text = rx.sub(rep_, text)
    return text


def git_summary(path):
    """Klasörün git durumunu kısa özetler; git yoksa ''."""
    try:
        cur = Path(path).resolve()
        while True:
            if (cur / ".git").exists():
                break
            if cur.parent == cur:
                return ""
            cur = cur.parent
        kw = dict(cwd=str(cur), timeout=8, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        st = subprocess.check_output(["git", "status", "--short"], **kw).decode("utf-8", "replace").strip()
        br = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], **kw).decode("utf-8", "replace").strip()
        lg = subprocess.check_output(["git", "log", "-3", "--pretty=%h %s"], **kw).decode("utf-8", "replace").strip()
        res = "🌿 Git · dal: %s\n" % br
        if not st:
            res += "Çalışma klasörü temiz (değişiklik yok)."
        else:
            ls = st.splitlines()
            res += "%d dosya değişti:\n" % len(ls) + "\n".join("  " + l for l in ls[:12])
            if len(ls) > 12:
                res += "\n  … ve %d dosya daha" % (len(ls) - 12)
        if lg:
            res += "\n\nSon kayıtlar:\n" + "\n".join("  " + l for l in lg.splitlines())
        return res
    except Exception:
        return ""

# --------------------------------------------------------------------------- Telegram
class TgError(Exception):
    pass


class Telegram:
    def __init__(self, token, base="https://api.telegram.org"):
        self.token = token
        self.base = base.rstrip("/")

    def _url(self, method):
        return "%s/bot%s/%s" % (self.base, self.token, method)

    def _read(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise TgError("HTTP %s" % e.code)
        if not res.get("ok"):
            raise TgError("%s (%s)" % (res.get("description"), res.get("error_code")))
        return res["result"]

    def call(self, method, params=None, timeout=40):
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(self._url(method), data, {"Content-Type": "application/json"})
        return self._read(req, timeout)

    def upload(self, method, fields, field_name, filename, content, mime="application/octet-stream"):
        boundary = "----agk" + uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                 % (boundary, field_name, filename, mime)).encode("utf-8")
        body += content + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(self._url(method), body,
                                     {"Content-Type": "multipart/form-data; boundary=" + boundary})
        return self._read(req, 90)

    # --- yardımcılar
    @staticmethod
    def _markup(m):
        if not m:
            return None
        return {"inline_keyboard": m} if isinstance(m, list) else m

    def send_text(self, chat_id, text, markup=None):
        text = redact_secrets((text or "").strip()) or "(boş)"
        if len(text) > 11000:
            head = text[:3000] + "\n\n… (cevap uzun, tamamı ekteki dosyada)"
            self._send_chunks(chat_id, head, markup)
            self.send_document(chat_id, "cevap.md", text.encode("utf-8"))
            return
        self._send_chunks(chat_id, text, markup)

    def _send_chunks(self, chat_id, text, markup=None):
        while text:
            if len(text) <= 3900:
                part, text = text, ""
            else:
                cut = text.rfind("\n", 0, 3900)
                if cut < 1500:
                    cut = 3900
                part, text = text[:cut], text[cut:].lstrip("\n")
            params = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if not text and markup:
                params["reply_markup"] = self._markup(markup)
            for attempt in range(3):
                try:
                    self.call("sendMessage", params)
                    break
                except (TgError, urllib.error.URLError, OSError) as e:
                    log("Telegram gönderim hatası: %s" % e)
                    time.sleep(2 + attempt * 3)

    def edit_text(self, chat_id, message_id, text, markup=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:3900],
                  "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = self._markup(markup)
        try:
            return self.call("editMessageText", params)
        except TgError as e:
            if "not modified" in str(e).lower():
                return None
            raise

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download(self, file_path, dest):
        url = "%s/file/bot%s/%s" % (self.base, self.token, file_path)
        with urllib.request.urlopen(url, timeout=180) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)

    def send_document(self, chat_id, filename, content, caption=""):
        return self.upload("sendDocument", {"chat_id": chat_id, "caption": caption[:900]},
                           "document", filename, content)

    def send_photo(self, chat_id, filename, content, caption=""):
        try:
            return self.upload("sendPhoto", {"chat_id": chat_id, "caption": caption[:900]},
                               "photo", filename, content, "image/png")
        except TgError:
            return self.send_document(chat_id, filename, content, caption)


# --------------------------------------------------------------------------- Arayüz tarafı (sayfaya enjekte edilen JS)
JS_VERSION = 13
JS_SRC = r"""
(function(){
if (window.__agk && window.__agk.v === %d) return;
let SBL = 150;   // kenar çubuğunun sağında kalan alan başlangıcı (otomatik ölçülür)
let SBR = 560;   // kenar çubuğu satırlarının en sağ sınırı (otomatik ölçülür)
function updSB(){
  let m = 0;
  const els = document.querySelectorAll('button,a,div,[role="button"]');
  for (const el of els){
    const r = el.getBoundingClientRect();
    if (r.left > 120 || r.width < 100 || r.height < 18 || r.height > 60) continue;
    const t = (el.innerText || '').trim().toLowerCase();
    if (t === 'new conversation' || t === 'conversation history' || t === 'automations' || t === 'customizations'){
      if (r.right > m) m = r.right;
    }
  }
  if (m > 0){ SBL = Math.max(150, Math.round(m) - 10); SBR = Math.round(m) + 30; }
}
const NAV = ['new conversation','conversation history','automations','customizations','projects','settings','search','yeni konuşma'];
function vis(el){
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
}
function animated(el){
  const nodes = [el].concat(Array.from(el.querySelectorAll('*')));
  for (const n of nodes){
    const s = getComputedStyle(n);
    if (s.animationName && s.animationName !== 'none' && s.animationPlayState === 'running' &&
        /infinite/.test(s.animationIterationCount)) return true;
    if (n.tagName && /^(animate|animatetransform)$/i.test(n.tagName) && n.getAttribute('repeatCount') === 'indefinite') return true;
    const cl = (n.getAttribute && n.getAttribute('class')) || '';
    if (/(^|[\s-])(spin|spinner|loader|loading)([\s-]|$)/i.test(String(cl)) && n.tagName && /^(svg|span|i|div)$/i.test(n.tagName)) return true;
  }
  return false;
}
function firstTextLeft(el){
  const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())){
    if (n.nodeValue.trim()){
      const rg = document.createRange(); rg.selectNodeContents(n);
      const b = rg.getBoundingClientRect();
      if (b.width > 0) return {l: b.left, r: b.right};
    }
  }
  return null;
}
function lastTextRight(el){
  const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let n, res = null;
  while ((n = w.nextNode())){
    if (n.nodeValue.trim()){
      const rg = document.createRange(); rg.selectNodeContents(n);
      const b = rg.getBoundingClientRect();
      if (b.width > 0) res = b.right;
    }
  }
  return res;
}
function icons(el){ return Array.from(el.querySelectorAll('svg,img,i,[class*="icon"]')).filter(function(i){ const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; }); }
function hasLeadingIcon(el){
  const t = firstTextLeft(el); if (!t) return false;
  return icons(el).some(function(i){ return i.getBoundingClientRect().right <= t.l + 3; });
}
function hasTrailingIcon(el){
  const t = lastTextRight(el); if (t === null) return false;
  return icons(el).some(function(i){ return i.getBoundingClientRect().left >= t - 3; });
}
const AGE = /^(\d+\s?(s|m|h|d|w|mo|y|sn|dk|sa|g|hf|ay)( ago)?|now|just now|şimdi|az önce)$/i;
const A = { v: %d, els: [] };
A.rows = function(init){
  const out = [], chosen = [];
  A.els = [];
  let project = init || '';
  const all = document.body.querySelectorAll('*');
  for (const el of all){
    let inside = false;
    for (const c of chosen){ if (c.contains(el)) { inside = true; break; } }
    if (inside) continue;
    const r = el.getBoundingClientRect();
    if (r.height < 18 || r.height > 60 || r.width < 100) continue;
    if (r.left > 120 || r.right > SBR) continue;
    if (!vis(el)) continue;
    const t = (el.innerText || '').trim();
    if (!t) continue;
    const lines = t.split('\n').map(function(s){return s.trim();}).filter(Boolean);
    if (lines.length > 3) continue;
    if (NAV.indexOf(lines[0].toLowerCase()) >= 0){ chosen.push(el); continue; }
    const last = lines[lines.length - 1];
    const age = (lines.length > 1 && AGE.test(last)) ? last : '';
    const run = animated(el);
    chosen.push(el);
    let isConv;
    if (hasLeadingIcon(el)) isConv = false;
    else if (age || run) isConv = true;
    else isConv = hasTrailingIcon(el);
    A.els.push(el);
    if (isConv){
      out.push({kind:'conv', title: lines[0], project: project, age: age, running: run, idx: A.els.length - 1});
    } else {
      project = lines[0];
      out.push({kind:'folder', title: lines[0], project: lines[0], age: '', running: false, idx: A.els.length - 1});
    }
  }
  return out;
};
A.scroller = function(){
  let best = null, bh = 0;
  for (const n of document.querySelectorAll('*')){
    const r = n.getBoundingClientRect();
    if (r.left > 140 || r.width < 120 || r.width > SBR + 40 || r.height < 80) continue;
    if (n.scrollHeight <= n.clientHeight + 8) continue;
    const s = getComputedStyle(n);
    if (/(auto|scroll)/.test(s.overflowY) && n.clientHeight > bh && vis(n)) { best = n; bh = n.clientHeight; }
  }
  return best;
};
A.sbInfo = function(){
  const sc = A.scroller();
  return sc ? {top: sc.scrollTop, sh: sc.scrollHeight, ch: sc.clientHeight} : null;
};
A.sbTo = function(y){
  const sc = A.scroller();
  if (!sc) return null;
  sc.scrollTop = y;
  try { sc.dispatchEvent(new Event('scroll')); } catch (e) {}
  return {top: sc.scrollTop, sh: sc.scrollHeight, ch: sc.clientHeight};
};
const MORE = /^(show more|view more|see more|see all|view all|load more|daha fazla|tümünü göster|devamını göster|\+?\s?\d+\s+(more|daha))/i;
A.expandMore = function(){
  let n = 0;
  const list = document.querySelectorAll('button,[role="button"],a,div,span');
  for (const el of list){
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10 || r.height > 50 || r.left > 140 || r.right > SBR) continue;
    const t = (el.innerText || '').trim();
    if (t && t.length < 30 && MORE.test(t) && el.children.length < 3 && vis(el)){ el.click(); n++; if (n > 6) break; }
  }
  return n;
};
A.busyWhy = function(){
  const sb = A.stopEl();
  if (sb) return 'durdur-dugmesi:' + String((sb.getAttribute('aria-label') || sb.getAttribute('title') || sb.innerText || '?')).trim().slice(0, 30);
  const t = A.readPane().text.trim().split('\n').slice(-3).join(' ');
  if (/(^|\s)(working|generating|thinking)\.*(\s|$)/i.test(t) && !/(finished|worked for)\s*$/i.test(t)) return 'metin:' + t.slice(-60);
  return '';
};
A.busy = function(){ return !!A.busyWhy(); };
A.center = function(el){
  el.scrollIntoView({block:'center', inline:'nearest'});
  const r = el.getBoundingClientRect();
  return {x: Math.round(r.left + Math.min(80, r.width/2)), y: Math.round(r.top + r.height/2)};
};
A.target = function(project, title){
  const rows = A.rows();
  let best = null;
  for (const r of rows){
    if (r.kind !== 'conv') continue;
    if (r.title === title && r.project === project){ best = r; break; }
    if (!best && r.title === title) best = r;
  }
  if (!best){
    for (const r of rows){ if (r.kind === 'conv' && title && r.title.indexOf(title) === 0){ best = r; break; } }
  }
  return best ? A.center(A.els[best.idx]) : null;
};
A.pane = function(){
  const cx = SBL + (innerWidth - SBL) * 0.4, cy = innerHeight * 0.45;
  const el = document.elementFromPoint(cx, cy);
  let sc = null;
  for (let n = el; n && n !== document.documentElement; n = n.parentElement){
    const s = getComputedStyle(n);
    if (/(auto|scroll)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 20 &&
        n.clientWidth > 300 && n.getBoundingClientRect().left > SBL) sc = n;
  }
  if (sc) return sc;
  // kaydırılabilir kap yoksa (kısa sohbet): mesaj kutusunu ve kenar çubuğunu içermeyen en büyük kap
  const comp = A.composerEl();
  sc = el || document.body;
  for (let n = el; n && n.parentElement && n !== document.body; n = n.parentElement){
    const r = n.getBoundingClientRect();
    if (r.left <= SBL || (comp && n.contains(comp))) break;
    sc = n;
  }
  return sc;
};
A.headerText = function(){
  const parts = [];
  for (const el of document.body.querySelectorAll('*')){
    const r = el.getBoundingClientRect();
    if (r.top < -2 || r.top > 120 || r.left < SBL || r.height > 60 || r.height < 8 || r.width < 20) continue;
    if (!vis(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length < 300) parts.push(t);
  }
  return parts.join(' | ').slice(0, 3000);
};
A.restorePane = function(){
  if (A._top !== undefined){
    const sc = A.pane();
    if (sc) sc.scrollTop = A._top;
    A._top = undefined;
  }
  return true;
};
A.readPane = function(scroll){
  const sc = A.pane();
  if (scroll){
    if (A._top === undefined) A._top = sc.scrollTop;   // kullanıcının kaydırma yerini hatırla
    sc.scrollTop = sc.scrollHeight;
  }
  return {text: sc.innerText || '', tag: sc.tagName, cls: String(sc.className).slice(0,120), sh: sc.scrollHeight};
};
A.composerEl = function(){
  const c = Array.from(document.querySelectorAll('textarea,[contenteditable=""],[contenteditable="true"],[contenteditable="plaintext-only"],[role="textbox"]'))
    .filter(function(e){ const r = e.getBoundingClientRect(); return vis(e) && r.left > SBL && r.width > 250; });
  c.sort(function(a,b){ return b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom; });
  return c[0] || null;
};
A.prepComposer = function(clear){
  const c = A.composerEl();
  if (!c) return null;
  c.focus();
  if (clear){
    if (c.tagName === 'TEXTAREA' || c.tagName === 'INPUT'){ c.select(); document.execCommand('delete'); }
    else { document.execCommand('selectAll', false, null); document.execCommand('delete', false, null); }
  } else if (c.tagName !== 'TEXTAREA' && c.tagName !== 'INPUT'){
    const sel = getSelection(); const rg = document.createRange();
    rg.selectNodeContents(c); rg.collapse(false); sel.removeAllRanges(); sel.addRange(rg);
  }
  const r = c.getBoundingClientRect();
  return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2), tag: c.tagName};
};
A.attState = function(){
  const c = A.composerEl();
  if (!c) return {n: 0, h: 0};
  let root = c;
  for (let i = 0; i < 6 && root.parentElement && root.parentElement !== document.body; i++){
    if (root.parentElement.getBoundingClientRect().height > 420) break;
    root = root.parentElement;
  }
  const q = root.querySelectorAll('img,canvas,video,[class*="attach" i],[class*="thumb" i],[class*="preview" i],[class*="chip" i],[class*="file" i]');
  return {n: q.length, h: root.innerHTML.length};
};
A.guard = function(on){
  if (on && !window.__agkGuard){
    window.__agkGuard = function(e){ e.preventDefault(); };
    window.addEventListener('dragover', window.__agkGuard, false);
    window.addEventListener('drop', window.__agkGuard, false);
  } else if (!on && window.__agkGuard){
    window.removeEventListener('dragover', window.__agkGuard, false);
    window.removeEventListener('drop', window.__agkGuard, false);
    window.__agkGuard = null;
  }
  return true;
};
function mkFile(name, mime, b64){
  const bin = atob(b64); const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const dt = new DataTransfer(); dt.items.add(new File([u8], name, {type: mime}));
  return dt;
}
A.synthPaste = function(name, mime, b64){
  const c = A.composerEl(); if (!c) return false;
  c.focus();
  c.dispatchEvent(new ClipboardEvent('paste', {clipboardData: mkFile(name, mime, b64), bubbles: true, cancelable: true}));
  return true;
};
A.synthDrop = function(name, mime, b64){
  const c = A.composerEl(); if (!c) return false;
  const dt = mkFile(name, mime, b64);
  const r = c.getBoundingClientRect(); const x = r.left + r.width/2, y = r.top + r.height/2;
  ['dragenter', 'dragover', 'drop'].forEach(function(t){
    c.dispatchEvent(new DragEvent(t, {dataTransfer: dt, bubbles: true, cancelable: true, clientX: x, clientY: y}));
  });
  return true;
};
A.composerText = function(){
  const c = A.composerEl();
  if (!c) return null;
  return (c.tagName === 'TEXTAREA' || c.tagName === 'INPUT') ? c.value : (c.innerText || '');
};
function isRed(col){
  const m = (col || '').match(/[\d.]+/g);
  if (!m || m.length < 3) return false;
  const a = m.length > 3 ? Number(m[3]) : 1;
  return a > 0.3 && Number(m[0]) > 170 && Number(m[1]) < 110 && Number(m[2]) < 110;
}
A.stopEl = function(){
  const btns = Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis);
  for (const b of btns){
    const r0 = b.getBoundingClientRect();
    if (r0.top < innerHeight * 0.8 || r0.left < SBL) continue;   // gerçek durdur düğmesi mesaj kutusunun yanındadır
    const t = ((b.getAttribute('aria-label')||'') + ' ' + (b.getAttribute('title')||'') + ' ' + (b.innerText||'')).toLowerCase();
    if (/\b(stop|durdur)\b/.test(t)) return b;
  }
  for (const b of btns){
    const r = b.getBoundingClientRect();
    if (r.top < innerHeight * 0.9 || r.left < SBL) continue;
    const nodes = [b].concat(Array.from(b.querySelectorAll('*')));
    for (const n of nodes){
      const s = getComputedStyle(n);
      if (isRed(s.backgroundColor) || isRed(s.fill)) return b;
    }
  }
  return null;
};
A.stopCenter = function(){
  const b = A.stopEl();
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
};
const APPR = /^(approve|allow|accept|reject|deny|decline|always allow|allow once|allow this|onayla|izin ver|reddet|kabul)/i;
A.approvals = function(){
  const out = [];
  const btns = Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis);
  for (const b of btns){
    const r = b.getBoundingClientRect();
    if (r.left < SBL) continue;
    const t = (b.innerText || b.getAttribute('aria-label') || '').trim().split('\n')[0].trim();
    if (t && t.length < 40 && APPR.test(t)) out.push({text: t, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)});
  }
  return out;
};
A.approvalPos = function(text){
  const l = A.approvals();
  for (const b of l){ if (b.text === text) return {x: b.x, y: b.y}; }
  return null;
};
A.folderPos = function(project){
  const rows = A.rows();
  for (const r of rows){
    if (r.kind === 'folder' && r.title === project){
      const b = A.els[r.idx].getBoundingClientRect();
      return {x: Math.round(b.left + b.width/2), y: Math.round(b.top + b.height/2)};
    }
  }
  return null;
};
A.newConvPos = function(project){
  const rows = A.rows();
  for (const r of rows){
    if (r.kind === 'folder' && r.title === project){
      const b = A.els[r.idx].getBoundingClientRect();
      const cands = Array.from(document.querySelectorAll('button,[role="button"],svg,[class*="icon"]')).filter(function(e){
        const q = e.getBoundingClientRect();
        return q.width > 0 && q.height > 0 && q.top >= b.top - 4 && q.bottom <= b.bottom + 4 && q.left > b.left + b.width * 0.5 && q.right <= SBR;
      });
      let best = null;
      for (const c of cands){
        const lab = ((c.getAttribute('aria-label') || '') + ' ' + (c.getAttribute('title') || '')).toLowerCase();
        if (/new|yeni|add|ekle|\+/.test(lab)) { best = c; break; }
      }
      if (!best && cands.length) best = cands.sort(function(a, c){ return c.getBoundingClientRect().left - a.getBoundingClientRect().left; })[0];
      if (!best) return null;
      const q = best.getBoundingClientRect();
      return {x: Math.round(q.left + q.width/2), y: Math.round(q.top + q.height/2)};
    }
  }
  return null;
};
A.newConvClick = function(project){
  const rows = A.rows();
  for (const r of rows){
    if (r.kind === 'folder' && r.title === project){
      let n = A.els[r.idx];
      for (let i = 0; i < 6 && n; i++, n = n.parentElement){
        const a = n.querySelector('a[aria-label*="New Conversation" i],button[aria-label*="New Conversation" i],a[aria-label*="Yeni" i]');
        if (a){ a.click(); return true; }
      }
    }
  }
  return false;
};
A.navNewClick = function(){
  const all = document.querySelectorAll('button,[role="button"],a,div,span');
  for (const el of all){
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.left > 120 || r.right > SBR || r.height > 60) continue;
    const t = (el.innerText || '').trim().toLowerCase().replace(/^\+\s*/, '');
    if (t === 'new conversation' || t === 'yeni konuşma'){
      (el.closest('button,[role="button"],a') || el).click();
      return true;
    }
  }
  return false;
};
A.navNewPos = function(){
  const all = document.querySelectorAll('button,[role="button"],a,div,span');
  for (const el of all){
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.left > 120 || r.right > SBR || r.height > 60) continue;
    const t = (el.innerText || '').trim().toLowerCase().replace(/^\+\s*/, '');
    if (t === 'new conversation' || t === 'yeni konuşma') return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
  }
  return null;
};
const MODEL = /(gemini|claude|gpt|flash|sonnet|opus|haiku|\bpro\b|\bo[34]\b|llama|deepseek|grok)/i;
A.modelBtn = function(){
  const cands = Array.from(document.querySelectorAll('button,[role="button"],div,span')).filter(function(e){
    if (!vis(e)) return false;
    const r = e.getBoundingClientRect();
    if (r.left < SBL || r.top < innerHeight * 0.55 || r.height > 40 || r.width > 320) return false;
    const t = (e.innerText || '').trim();
    return t && t.length < 60 && t.split('\n').length === 1 && MODEL.test(t) && e.children.length < 4;
  });
  let best = null;
  for (const e of cands){
    const b = e.closest('button,[role="button"]') || e;
    if (!best || b.getBoundingClientRect().width >= best.getBoundingClientRect().width) best = b;
  }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  return {text: (best.innerText || '').trim(), x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
};
A.modelOptions = function(){
  const out = [], seen = {};
  const els = Array.from(document.querySelectorAll('[role="menuitem"],[role="option"],[role="menuitemradio"],[role="menu"] *,[role="listbox"] *,[data-radix-popper-content-wrapper] *'));
  for (const e of els){
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.height < 14 || r.height > 70 || r.left < SBL - 20) continue;
    const t = (e.innerText || '').trim().split('\n')[0].trim();
    if (!t || t.length > 50 || !MODEL.test(t) || seen[t]) continue;
    if (e.children.length > 3 && !/menuitem|option/.test(e.getAttribute('role') || '')) continue;
    seen[t] = 1;
    out.push({text: t, x: Math.round(r.left + Math.min(60, r.width/2)), y: Math.round(r.top + r.height/2)});
  }
  return out;
};
A.ctl = function(kind){
  const c = A.composerEl();
  if (!c) return null;
  const cr = c.getBoundingClientRect();
  const btns = Array.from(document.querySelectorAll('button,[role="button"]')).filter(function(e){
    if (!vis(e)) return false;
    const r = e.getBoundingClientRect();
    return r.left > SBL && r.height < 56 && r.width < 420 && r.width > 12;
  });
  let best = null, bd = 1e9;
  for (const e of btns){
    const r = e.getBoundingClientRect();
    const t = (e.innerText || '').trim().replace(/\s+/g, ' ');
    if (kind === 'proj'){
      if (!t || t.length > 50 || r.bottom > cr.top + 6 || cr.top - r.bottom > 160 || !e.querySelector('svg')) continue;
      const d = cr.top - r.bottom;
      if (d < bd){ bd = d; best = e; }
    } else if (kind === 'env'){
      if (!/^(local|new worktree|worktree|remote)/i.test(t) || r.top < cr.bottom - 6 || r.top - cr.bottom > 120) continue;
      best = e; break;
    } else if (kind === 'model'){
      if (!t || t.length > 60 || !MODEL.test(t) || r.top < cr.top - 4 || r.top > cr.bottom + 140) continue;
      if (!best || r.width >= best.getBoundingClientRect().width) best = e;
    }
  }
  if (!best) return null;
  const q = best.getBoundingClientRect();
  return {text: (best.innerText || '').trim().replace(/\s+/g, ' '), x: Math.round(q.left + q.width/2), y: Math.round(q.top + q.height/2)};
};
A.menuAll = function(){
  const out = [], seen = {};
  const roots = Array.from(document.querySelectorAll('[role="menu"],[role="listbox"],[data-radix-popper-content-wrapper],[data-base-ui-popup],[popover],[class*="popover" i],[class*="dropdown" i]')).filter(vis);
  for (const root of roots){
    let items = Array.from(root.querySelectorAll('[role="menuitem"],[role="option"],[role="menuitemradio"]')).filter(vis);
    if (!items.length) items = Array.from(root.querySelectorAll('div,button,li,a')).filter(function(e){
      if (!vis(e) || e.children.length > 4) return false;
      const r = e.getBoundingClientRect();
      return r.height >= 18 && r.height <= 52 && (e.innerText || '').trim() && (e.innerText || '').trim().split('\n').length <= 2;
    });
    for (const e of items){
      const r = e.getBoundingClientRect();
      if (r.left < SBL - 20) continue;
      const t = (e.innerText || '').trim().split('\n')[0].trim();
      if (!t || t.length > 60 || seen[t]) continue;
      seen[t] = 1;
      out.push({text: t, x: Math.round(r.left + Math.min(70, r.width/2)), y: Math.round(r.top + r.height/2)});
    }
  }
  if (!out.length){
    // yedek: açılan listeyi "Search ..." kutusundan veya serbest yüzen kutudan bul
    let box = null;
    const inp = Array.from(document.querySelectorAll('input')).filter(function(i){ return vis(i) && /search|ara/i.test(i.placeholder || ''); })[0];
    if (inp){ box = inp; for (let i = 0; i < 6 && box.parentElement; i++){ box = box.parentElement; if (box.getBoundingClientRect().height > 120) break; } }
    if (box){
      for (const e of box.querySelectorAll('div,button,li,a,span')){
        if (!vis(e) || e.children.length > 4) continue;
        const r = e.getBoundingClientRect();
        if (r.height < 18 || r.height > 52 || r.left < SBL - 20) continue;
        const t = (e.innerText || '').trim().split('\n')[0].trim();
        if (!t || t.length > 60 || seen[t]) continue;
        seen[t] = 1;
        out.push({text: t, x: Math.round(r.left + Math.min(70, r.width/2)), y: Math.round(r.top + r.height/2)});
      }
    }
  }
  return out;
};
A.menuPos = function(text){
  const l = A.menuAll();
  for (const o of l){ if (o.text === text) return {x: o.x, y: o.y}; }
  let best = null, ba = 1e9;
  for (const e of document.querySelectorAll('div,button,li,a,span')){
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.left < SBL - 20 || r.height < 14 || r.height > 60) continue;
    if ((e.innerText || '').trim() !== text) continue;
    const a = r.width * r.height;
    if (a < ba){ ba = a; best = r; }
  }
  return best ? {x: Math.round(best.left + Math.min(70, best.width/2)), y: Math.round(best.top + best.height/2)} : null;
};
A.optionPos = function(text){
  const l = A.modelOptions();
  for (const o of l){ if (o.text === text) return {x: o.x, y: o.y}; }
  return null;
};
const RETRY = /^(retry|try again|regenerate|tekrar dene|yeniden dene|yeniden oluştur)/i;
A.retryBtn = function(){
  const btns = Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis);
  let best = null;
  for (const b of btns){
    const r = b.getBoundingClientRect();
    if (r.left < SBL) continue;
    const t = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '')).trim();
    if (RETRY.test(t)) best = b;
  }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
};
A.watchStart = function(){
  if (A._mo) return true;
  let t = null;
  A._mo = new MutationObserver(function(){
    if (t) return;
    t = setTimeout(function(){ t = null; try { if (window.agkPing) window.agkPing('x'); } catch (e) {} }, 500);
  });
  A._mo.observe(document.body, {subtree: true, childList: true, characterData: true});
  return true;
};
A.health = function(){
  const rows = A.rows();
  const comp = A.composerEl();
  const mb = A.modelBtn();
  const ap = A.approvals();
  return {
    folders: rows.filter(function(r){ return r.kind === 'folder'; }).length,
    convs: rows.filter(function(r){ return r.kind === 'conv'; }).length,
    running: rows.filter(function(r){ return r.running; }).length,
    scroller: !!A.scroller(), composer: !!comp, stop: !!A.stopEl(),
    fileInputs: document.querySelectorAll('input[type=file]').length,
    model: mb ? mb.text : null, retry: !!A.retryBtn(), approvals: ap.length,
    header: A.headerText().slice(0, 120), size: [innerWidth, innerHeight]
  };
};
A.diag = function(){
  const rows = A.rows();
  const comp = A.composerEl();
  const st = A.stopEl();
  const pn = A.readPane();
  let html = '';
  if (A.els.length){
    let n = A.els[0];
    for (let i = 0; i < 4 && n.parentElement; i++) n = n.parentElement;
    html = n.outerHTML.slice(0, 7000);
  }
  return {
    url: location.href, title: document.title, size: [innerWidth, innerHeight],
    rows: rows, sb: A.sbInfo(), header: A.headerText(), fileInputs: document.querySelectorAll('input[type=file]').length, attState: A.attState(),
    composerBox: (function(){ const c = A.composerEl(); if (!c) return ''; let r = c; for (let i = 0; i < 4 && r.parentElement; i++) r = r.parentElement; return r.outerHTML.slice(0, 3500); })(),
    composer: comp ? {tag: comp.tagName, cls: String(comp.className).slice(0,150), text: (comp.innerText||comp.value||'').slice(0,100)} : null,
    stop: st ? {tag: st.tagName, cls: String(st.className).slice(0,150)} : null,
    pane: {tag: pn.tag, cls: pn.cls, sh: pn.sh, len: pn.text.length, tail: pn.text.slice(-2500)},
    sidebarHtml: html
  };
};
for (const k of Object.keys(A)){
  if (typeof A[k] === 'function'){
    const f = A[k];
    A[k] = function(){ updSB(); return f.apply(A, arguments); };
  }
}
window.__agk = A;
})();
""" % (JS_VERSION, JS_VERSION)


# --------------------------------------------------------------------------- CDP (Chrome DevTools Protocol)
class UIError(Exception):
    """Arayüzde beklenen öğe bulunamadı (bağlantı sorunu değil)."""


class CDPError(Exception):
    pass


class CDP:
    def __init__(self, port, markers):
        self.port = int(port)
        self.markers = [m.strip().lower() for m in markers.split(",") if m.strip()]
        self.ws = None
        self.msgid = 0
        self.ping = 0.0
        self.io = threading.RLock()

    def http_json(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/json" % self.port, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    def available(self):
        try:
            self.http_json()
            return True
        except Exception:
            return False

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    def connected(self):
        return self.ws is not None

    def connect(self):
        import websocket  # websocket-client
        self.close()
        try:
            data = self.http_json()
        except Exception as e:
            raise CDPError("Antigravity debug portuna ulaşılamıyor (%s)" % e)
        pages = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                 and not str(t.get("url", "")).startswith("devtools://")]
        if not pages:
            raise CDPError("Açık pencere bulunamadı")
        first = None
        for t in pages:
            try:
                ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
            except Exception as e:
                log("CDP bağlantı hatası: %s" % e)
                continue
            self.ws = ws
            if first is None:
                first = ws
            try:
                txt = (self.ev("(document.body && document.body.innerText || '').slice(0, 30000)") or "").lower()
            except Exception:
                txt = ""
            if any(m in txt for m in self.markers):
                self._on_connected()
                return
            if ws is not first:
                try:
                    ws.close()
                except Exception:
                    pass
        if first is None:
            raise CDPError("Hiçbir pencereye bağlanılamadı")
        self.ws = first
        self._on_connected()

    def _on_connected(self):
        try:
            self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception:
            pass
        try:
            self.send("Runtime.addBinding", {"name": "agkPing"})
        except Exception:
            pass

    def _event(self, msg):
        if msg.get("method") == "Runtime.bindingCalled" and (msg.get("params") or {}).get("name") == "agkPing":
            self.ping = time.time()

    def pump(self, secs):
        """Sayfadan gelen olayları (DOM değişti uyarısı) dinler; başka bir işlem sürüyorsa hemen döner."""
        if not self.ws or not self.io.acquire(False):
            time.sleep(min(secs, 0.2))
            return
        try:
            self.ws.settimeout(secs)
            msg = json.loads(self.ws.recv())
            if "id" not in msg:
                self._event(msg)
        except Exception as e:
            if "timed out" not in str(e).lower() and e.__class__.__name__ != "WebSocketTimeoutException":
                self.close()
        finally:
            self.io.release()

    def press_key(self, key, code, vk):
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        self.send("Input.dispatchKeyEvent", dict(base, type="keyDown"))
        self.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def send(self, method, params=None, timeout=20):
        with self.io:
            return self._send(method, params, timeout)

    def _send(self, method, params=None, timeout=20):
        if not self.ws:
            raise CDPError("bağlı değil")
        self.msgid += 1
        mid = self.msgid
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            end = time.time() + timeout
            while time.time() < end:
                self.ws.settimeout(max(1.0, end - time.time()))
                msg = json.loads(self.ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise CDPError(str(msg["error"]))
                    return msg.get("result", {})
                if "id" not in msg:
                    self._event(msg)
        except CDPError:
            raise
        except Exception as e:
            self.close()
            raise CDPError("bağlantı koptu: %s" % e)
        self.close()
        raise CDPError("zaman aşımı: " + method)

    def ev(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        ex = r.get("exceptionDetails")
        if ex:
            desc = (ex.get("exception") or {}).get("description") or ex.get("text")
            raise CDPError("JS hatası: %s" % desc)
        return (r.get("result") or {}).get("value")

    def agk(self, call):
        if not self.ev("!!(window.__agk && window.__agk.v === %d)" % JS_VERSION):
            self.ev(JS_SRC)
            try:
                self.ev("window.__agk.watchStart()")
            except CDPError:
                pass
        return self.ev("window.__agk." + call)

    def click(self, x, y):
        self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    def insert_text(self, text):
        self.send("Input.insertText", {"text": text})

    def press_enter(self):
        base = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
        self.send("Input.dispatchKeyEvent", dict(base, type="keyDown", text="\r"))
        self.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def set_input_files(self, paths):
        r = self.send("Runtime.evaluate", {"expression":
            "(function(){const l = Array.from(document.querySelectorAll('input[type=file]')); return l.length ? l[l.length - 1] : null;})()"})
        obj = (r.get("result") or {}).get("objectId")
        if not obj:
            return False
        self.send("DOM.setFileInputFiles", {"files": paths, "objectId": obj})
        return True

    def drag_files(self, paths, x, y):
        data = {"items": [], "files": paths, "dragOperationsMask": 1}
        for t in ("dragEnter", "dragOver", "drop"):
            self.send("Input.dispatchDragEvent", {"type": t, "x": x, "y": y, "data": data})
            time.sleep(0.15)

    def screenshot(self):
        r = self.send("Page.captureScreenshot", {"format": "png"}, timeout=30)
        return base64.b64decode(r["data"])


# --------------------------------------------------------------------------- cevap ayıklama
STEP_RE = re.compile(r"^(Worked for|Thought for|Analyzed|Edited|Created|Viewed|Ran |Read |Searched|Deleted|Wrote|Listed|Check |Starting )|\bfinished$|^\d+ files? changed", re.I)
NOISE_RE = re.compile(r"^(\d+ files? changed.*|Review|Working|Generating\.*|Copy|Retry|Ask anything.*|just now|\d+\w{1,2} ago)$", re.I)


def extract_final(text):
    """Sohbet panelinin metninden son cevabı ayıklar."""
    lines = [l.rstrip() for l in (text or "").splitlines()]
    idx = -1
    for i, l in enumerate(lines):
        if STEP_RE.match(l.strip()):
            idx = i
    chunk = lines[idx + 1:] if idx >= 0 else lines
    chunk = [l for l in chunk if not NOISE_RE.match(l.strip())]
    ans = "\n".join(chunk).strip()
    ans = re.sub(r"\n{3,}", "\n\n", ans)
    if len(ans) < 40:  # ayıklama tutmadıysa sohbetin sonunu gönder
        ans = re.sub(r"\n{3,}", "\n\n", (text or "").strip())[-3000:]
    if len(ans) > 30000:
        ans = "…\n" + ans[-30000:]
    return ans


def conv_key(r):
    return "%s||%s" % (r.get("project", ""), r.get("title", ""))


def label(r):
    return ("%s / %s" % (r["project"], r["title"])) if r.get("project") else r["title"]



# --------------------------------------------------------------------------- Köprü
IS_WIN = os.name == "nt"
BTN_PROJ, BTN_DONE, BTN_RUN = "📁 Projeler", "✅ Biten işler", "⏳ Çalışanlar"
BTN_SCHED, BTN_PC = "⏰ Zamanlı", "🖥 Bilgisayar"
BTN_NEW, BTN_TOOLS = "➕ Yeni sohbet", "🧰 Araçlar"
KEYBOARD = {"keyboard": [[{"text": BTN_PROJ}, {"text": BTN_NEW}],
                         [{"text": BTN_DONE}, {"text": BTN_RUN}],
                         [{"text": BTN_PC}, {"text": BTN_TOOLS}]],
            "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "Mesajını yaz…"}
LINE = "━━━━━━━━━━━━━━━━"


def bar(frac, n=10):
    """0-1 arası oranı ▓▓▓░░ çubuğu olarak çizer."""
    frac = max(0.0, min(1.0, frac))
    k = int(round(frac * n))
    return "▓" * k + "░" * (n - k)


def lamp(frac):
    return "🟢" if frac < 0.6 else ("🟡" if frac < 0.85 else "🔴")
SELECT_TTL = 3 * 3600   # seçili sohbet 3 saat sonra unutulur (yanlış sohbete yazmayı önler)
NEW_GENERAL = "(soldaki New Conversation)"
WIZ_MARK = "(yeni sohbet sihirbazı)"
MODEL_PY = re.compile(r"(gemini|claude|gpt|flash|sonnet|opus|haiku|\bpro\b|\bo[34]\b|llama|deepseek|grok)", re.I)
NEW_TTL = 600           # "yeni sohbet" modu 10 dk geçerli
DANGER_TTL = 600
MAX_DL = 20 * 1024 * 1024    # Telegram botlarının indirebildiği en büyük dosya
MAX_SEND = 50 * 1024 * 1024  # Telegram botlarının gönderebildiği en büyük dosya

HELP = (
    "🤖 Antigravity Köprüsü\n" + LINE + "\n\n"
    "Alttaki düğmeler:\n"
    "📁 Projeler → proje → sohbet seç\n"
    "➕ Yeni sohbet → model, çalışma yeri, proje sorulur\n"
    "✅ Biten işler → cevabını çek\n"
    "⏳ Çalışanlar → şu an çalışan işler\n"
    "🖥 Bilgisayar → durum, ekran, kilitle\n"
    "🧰 Araçlar → model, tekrar dene, sağlık, GitHub'a yayınla…\n"
    "🚀 /yayinla → İzlePro APK'sını GitHub'a yükler (önce kontrol, sonra onay). /surum 2.0.5 7 → sürümü artırır\n"
    "\n"
    "Yazmak: sohbeti seç, mesajını normal yaz.\n"
    "🎙 Sesli mesaj gönderirsen yazıya çevrilir, onayınla gönderilir.\n"
    "📎 Dosya (fotoğraf, PDF, Excel, Word, video…) gönderirsen sohbete ek olarak eklenir.\n"
    "   Altına açıklama yazarsan hemen gider, yazmazsan sonraki mesajınla gider.\n"
    "📤 Bilgisayardan dosya almak: /dosya <yol>  ya da cevabın altındaki 📤 düğmeleri.\n"
    "📲 WhatsApp'a göndermek (APK vb. büyük dosyalar): /wa <yol> ya da 📲 düğmesi. İlk kurulum: /whatsapp, kişi: /wakisi\n"
    "⏰ Zamanlamak: /zamanla 03:00 mesaj   /zamanla +30 mesaj   /zamanlar\n\n"
    "Uyarılar: iş bitince, onay beklenince ve takılınca haber veririm.\n\n"
    "🌿 /git (değişiklik özeti) · ⚠️ /hata (sohbetteki hatalar) · 🧪 /test (onayınla çalışır)\n"
    "🔐 Cevaplardaki API anahtarı/token otomatik maskelenir.\n\n"
    "🔔 /bildirim → iş bitti bildirimlerini kapat/ayarla; her bildirimde 🔕 ile o sohbeti sustur\n"
    "🐙 /hesap (GitHub hesabı değiştir) · 🚀 /depoyeni (klasörü yeni GitHub deposu yap) · 📤 /kodgonder (kodu güncelle)\n"
    "📂 /klasor → bilgisayardaki klasörlerde gezin, dosya al, projeni klasöre bağla\n"
    "🔐 /pin 1234 → tehlikeli işlere PIN koyar · 📦 /depoyukle <dosya> → GitHub son sürüme dosya ekler · 📋 /ozet 21:00 → günlük özet\n\n"
    "Komutlar: /pin /depoyukle /ozet /git /hata /test /menu /biten /durum /son /dur /model /tekrar /saglik /sessiz /guncelle /dosya /zamanla /zamanlar /pc /yeni /araclar /yayinla /surum /kilitle /ekran /teshis /yenidenbaslat"
)

COMMANDS = [
    {"command": "menu", "description": "Projeler ve sohbetler"},
    {"command": "biten", "description": "Biten işler"},
    {"command": "durum", "description": "Çalışan işler"},
    {"command": "son", "description": "Seçili sohbetin son cevabı"},
    {"command": "dur", "description": "Seçili sohbette işi durdur"},
    {"command": "dosya", "description": "Bilgisayardan dosya al"},
    {"command": "wa", "description": "Dosyayı WhatsApp'tan gönder"},
    {"command": "whatsapp", "description": "WhatsApp bağlantısı / QR"},
    {"command": "wakisi", "description": "WhatsApp kişileri"},
    {"command": "zamanla", "description": "Mesajı zamanla"},
    {"command": "zamanlar", "description": "Zamanlanmış mesajlar"},
    {"command": "pc", "description": "Bilgisayar durumu"},
    {"command": "ekran", "description": "Antigravity ekran görüntüsü"},
    {"command": "model", "description": "Seçili sohbette modeli değiştir"},
    {"command": "tekrar", "description": "Son işi tekrar dene (Retry)"},
    {"command": "yeni", "description": "Yeni sohbet (model, proje, klasör sorulur)"},
    {"command": "araclar", "description": "Tüm araçlar menüsü"},
    {"command": "kilitle", "description": "Bilgisayar ekranını kilitle"},
    {"command": "yayinla", "description": "APK'yı GitHub'a yayınla (önce kontrol, sonra onay)"},
    {"command": "surum", "description": "Proje sürümünü artır: /surum 2.0.5 7"},
    {"command": "bildirim", "description": "Bildirim ayarları / sustur"},
    {"command": "hesap", "description": "GitHub hesabını göster / değiştir"},
    {"command": "depoyeni", "description": "Klasörü yeni GitHub deposu yap"},
    {"command": "kodgonder", "description": "Proje kodunu GitHub'a güncelle"},
    {"command": "klasor", "description": "Klasörlerde gez, dosya al"},
    {"command": "pin", "description": "Tehlikeli işlere PIN koy"},
    {"command": "depoyukle", "description": "Dosyayı GitHub son sürüme yükle"},
    {"command": "ozet", "description": "Günlük özet (/ozet 21:00)"},
    {"command": "git", "description": "Git değişiklik özeti"},
    {"command": "hata", "description": "Sohbette görünen hatalar"},
    {"command": "test", "description": "Proje testlerini çalıştır (onaylı)"},
    {"command": "saglik", "description": "Köprü sağlık kontrolü"},
    {"command": "sessiz", "description": "Sessiz saat (gece bildirimi biriktir)"},
    {"command": "guncelle", "description": "Yeni sürümü indir"},
    {"command": "yenidenbaslat", "description": "Antigravity'yi köprülü yeniden aç"},
    {"command": "yardim", "description": "Yardım"},
]

DANGER_RE = re.compile(
    r"(\brm\s+-[a-z]*r|\bdel\s+/[sfq]|\brmdir\b|\bformat\s+[a-z]:|\bdrop\s+(table|database)\b|"
    r"\bgit\s+push\s+(-f\b|--force)|\bgit\s+reset\s+--hard|\bshutdown\b|\bformatla\b|"
    r"\b(hepsini|tümünü|tüm\s+\S+)\s+sil\b|\bkalıcı\s+olarak\s+sil|\bsil\b|"
    r"\bveritabanını\s+(sil|sıfırla)|\bdiski\s+(sil|temizle))", re.I)

PATH_RE = re.compile(r"(?:[A-Za-z]:\\[^\s\"'<>|*?\r\n]+|/(?:home|Users|mnt|tmp|var|opt)/[^\s\"'<>|*?\r\n]+)")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".gradle", ".dart_tool", ".idea", "intermediates", ".cache", "cache"}
BLOCK_WORDS = ("\\.ssh", "\\.aws", "\\.gnupg", "\\appdata\\", "ntuser.dat", "ayarlar.env", "login data",
               "\\cookies", "id_rsa", "\\.env", "zamanlanmis.json")


def hid(*parts):
    return hashlib.md5("||".join(parts).encode("utf-8")).hexdigest()[:8]


def norm(x):
    return re.sub(r"\s+", " ", x or "").strip().lower()


def header_matches(hdr, project, title):
    t = norm(title).rstrip("….").strip()
    h = norm(hdr)
    pre = t[:18]
    return bool(pre) and pre in h and (not project or norm(project) in h)


AGE_RE = re.compile(r"^(\d+)\s?(mo|ay|sn|sa|dk|hf|s|m|h|d|g|w|y)\b", re.I)
AGE_MULT = {"s": 1, "sn": 1, "m": 60, "dk": 60, "h": 3600, "sa": 3600, "d": 86400, "g": 86400,
            "w": 604800, "hf": 604800, "mo": 2592000, "ay": 2592000, "y": 31536000}


def age_seconds(age):
    """'5m', '2h', '3d', '1w', 'now' gibi sol liste yaşlarını saniyeye çevirir (bilinmiyorsa None)."""
    a = (age or "").strip().lower()
    if a in ("now", "just now", "şimdi", "az önce"):
        return 0
    m = AGE_RE.match(a)
    if not m:
        return None
    return int(m.group(1)) * AGE_MULT[m.group(2).lower()]


def disp_name(p):
    """Windows yolu ise yalnızca son klasör adını gösterir."""
    p = p or "(projesiz)"
    if re.match(r"^[A-Za-z]:[\\/]", p):
        return p.replace("/", "\\").rstrip("\\").split("\\")[-1] or p
    return p


def fmt_ago(sec):
    if sec is None:
        return ""
    if sec < 90:
        return "az önce"
    if sec < 3600:
        return "%d dk önce" % (sec // 60)
    if sec < 86400:
        return "%d sa önce" % (sec // 3600)
    if sec < 14 * 86400:
        return "%d gün önce" % (sec // 86400)
    if sec < 60 * 86400:
        return "%d hf önce" % (sec // 604800)
    return "%d ay önce" % (sec // 2592000)


def fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return "%d sn" % sec
    if sec < 3600:
        return "%d dk" % (sec // 60)
    return "%d sa %d dk" % (sec // 3600, (sec % 3600) // 60)


def short(x, n=45):
    x = (x or "").strip()
    return x if len(x) <= n else x[:n - 1] + "…"


def safe_name(n):
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n or "").strip(". ")
    return n[:120] or "dosya"


def human_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024


LIMIT_RE = re.compile(r"(quota|rate.?limit|limit (reached|exceeded)|resource.?exhausted|too many requests|\b429\b|"
                      r"overloaded|model (is )?unavailable|try again (later|in)|kota|limit(e| )?ula[sş]ıldı|limit doldu)", re.I)


def limit_wait_minutes(text):
    """'try again in 2 hours' gibi ifadeden bekleme süresini (dk) çıkarır; yoksa 60."""
    m = re.search(r"(\d+)\s*(hours?|hrs?|saat|minutes?|mins?|dakika|dk)", text or "", re.I)
    if not m:
        return 60
    n = int(m.group(1))
    return n * 60 if m.group(2).lower().startswith(("h", "s")) else max(1, n)


def idle_seconds():
    """Kullanıcının klavye/fareye en son dokunmasından beri geçen süre."""
    fake = os.environ.get("AGK_FAKE_IDLE")
    if fake:
        return float(fake)
    if not IS_WIN:
        return 0.0
    try:
        import ctypes

        class LII(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = LII()
        li.cbSize = ctypes.sizeof(li)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
        return ((ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF) - li.dwTime) / 1000.0
    except Exception:
        return 0.0


def fg_is_antigravity():
    """Ön planda Antigravity penceresi mi (kullanıcı ona bakıyor/yazıyor)?"""
    fake = os.environ.get("AGK_FAKE_FG")
    if fake:
        return fake == "1"
    if not IS_WIN:
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(h, buf, 512)
        return "antigravity" in buf.value.lower()
    except Exception:
        return False


def keep_awake_loop():
    """Windows'un uykuya geçmesini engeller (ekran kapanabilir)."""
    if not IS_WIN:
        return
    import ctypes
    while True:
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        except Exception:
            return
        time.sleep(30)


def path_allowed(p, roots):
    try:
        rp = Path(p).resolve()
    except Exception:
        return False
    low = str(rp).lower().replace("/", "\\")
    if any(b in low for b in BLOCK_WORDS):
        return False
    for r in roots:
        try:
            rr = Path(r).resolve()
            if rp == rr or rr in rp.parents:
                return True
        except Exception:
            pass
    return False


def find_paths(text):
    out = []
    for m in PATH_RE.findall(text or ""):
        m = m.rstrip(".,;:)]}'\"`")
        if m not in out:
            out.append(m)
    for m in re.findall(r'"([A-Za-z]:\\[^"]+)"', text or ""):
        if m not in out:
            out.append(m)
    return out


def newest_files(folder, n=10, depth=4):
    found = []
    base = Path(folder)
    for root, dirs, files in os.walk(str(base)):
        rel = Path(root).relative_to(base)
        if len(rel.parts) >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        for f in files:
            fp = Path(root) / f
            try:
                found.append((fp.stat().st_mtime, fp))
            except OSError:
                pass
    found.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in found[:n]]


ACT = [(re.compile(r"^Thought for", re.I), "💭"), (re.compile(r"^Worked for", re.I), "⏱"),
       (re.compile(r"^(Analyzed|Viewed|Read|Searched|Listed)\b", re.I), "🔍"),
       (re.compile(r"^(Edited|Created|Wrote|Deleted)\b", re.I), "✏️"),
       (re.compile(r"^(Ran |Check |Starting |Running)", re.I), "⚙️"),
       (re.compile(r"finished$", re.I), "✅")]


def format_activity(raw, n=28):
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    lines = [l for l in lines if not NOISE_RE.match(l)][-n:]
    out = []
    for l in lines:
        for rx, ic in ACT:
            if rx.search(l):
                out.append("%s %s" % (ic, l))
                break
        else:
            out.append(l)
    return "\n".join(out)


def summarize(raw):
    """Biten sohbetten tek bakışta özet satırları çıkarır."""
    lines = []
    m = re.search(r"(\d+)\s+files?\s+changed\s*\+?(\d+)?\s*[-−–]?\s*(\d+)?", raw or "")
    if m:
        l = "📝 %s dosya değişti" % m.group(1)
        if m.group(2):
            l += " (+%s −%s)" % (m.group(2), m.group(3) or 0)
        lines.append(l)
    ans = extract_final(raw)
    first = [x for x in ans.splitlines() if x.strip()][:2]
    if first:
        lines.append("💬 " + short(" ".join(first), 260))
    if LIMIT_RE.search((raw or "")[-700:]):
        lines.append("🚧 Limit / kota / yoğunluk uyarısı görünüyor")
    if re.search(r"\b(hata|error|failed|exception|başarısız)\b", ans, re.I) and \
            not re.search(r"\b0\s+(hata|error|issue)", ans, re.I):
        lines.append("⚠️ Cevapta hata/başarısızlık sözcüğü geçiyor")
    return lines, ans


def parse_when(arg, now=None):
    """'03:00 mesaj', 'yarın 09:30 mesaj', '+45 mesaj' -> (zaman, mesaj)."""
    now = now or time.time()
    lt = time.localtime(now)
    a = (arg or "").strip()
    m = re.match(r"^(yar[ıi]n\s+)?(\d{1,2})[:.](\d{2})\s+(.+)$", a, re.S | re.I)
    if m:
        h, mi = int(m.group(2)), int(m.group(3))
        if h > 23 or mi > 59:
            return None
        base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        if m.group(1):
            base += 86400
        elif base <= now:
            base += 86400
        return base, m.group(4).strip()
    m = re.match(r"^\+(\d+)\s*(dk|dak|sa|m|h)?\s+(.+)$", a, re.S | re.I)
    if m:
        unit = (m.group(2) or "dk").lower()
        return now + int(m.group(1)) * (3600 if unit in ("sa", "h") else 60), m.group(3).strip()
    return None


def safe_name_ext(name, mime, ext):
    if not Path(name).suffix:
        name += mimetypes.guess_extension(mime or "") or ext
    return name


def extract_media(msg):
    """Telegram mesajındaki tüm ek türlerini ortak biçime çevirir."""
    out = []
    ph = msg.get("photo")
    if ph:
        best = max(ph, key=lambda p: ((p.get("file_size") or 0), p.get("width", 0) * p.get("height", 0)))
        out.append({"kind": "fotoğraf", "file_id": best["file_id"], "name": "foto.jpg", "size": best.get("file_size")})
    kinds = (("animation", "animasyon", ".mp4"), ("document", "dosya", ""), ("video", "video", ".mp4"),
             ("audio", "ses", ".mp3"), ("voice", "ses mesajı", ".ogg"), ("video_note", "video notu", ".mp4"),
             ("sticker", "çıkartma", ".webp"))
    for key, kind, ext in kinds:
        o = msg.get(key)
        if not o or (key == "document" and msg.get("animation")):
            continue
        name = safe_name_ext(o.get("file_name") or (kind.replace(" ", "_") + ext), o.get("mime_type"), ext)
        out.append({"kind": kind, "file_id": o["file_id"], "name": name, "size": o.get("file_size")})
    return out


# --------------------------------------------------------------------------- WhatsApp (WhatsApp Web üzerinden)
WA_BLOCKED_EXT = {".exe", ".bat", ".cmd", ".msi", ".scr", ".vbs", ".com", ".pif", ".js"}

WA_JS = r"""
(function(){
const W = {};
function vis(el){ const r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) return false; const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none'; }
function pos(el){ const b = el.closest('button,[role="button"]') || el; const r = b.getBoundingClientRect(); return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}; }
function lab(el){ return ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.getAttribute('data-icon') || '') + ' ' + (el.getAttribute('data-testid') || '')).toLowerCase(); }
W.state = function(){
  if (document.querySelector('#pane-side,[data-icon="chats-filled"],[data-icon="new-chat-outline"],[aria-label="Chat list"],[aria-label="Sohbet listesi"],[data-testid="chat-list"]')) return 'chats';
  const t = (document.body.innerText || '').toLowerCase();
  if (document.querySelector('canvas,[data-ref]') && /(scan|qr|link with phone|log in|okut|tara|telefon numarası)/.test(t)) return 'qr';
  return 'loading';
};
W.error = function(){
  const d = document.querySelector('[role="dialog"],[data-animate-modal-popup="true"]');
  const t = d ? (d.innerText || '').toLowerCase() : '';
  if (/(invalid|geçersiz|not on whatsapp|whatsapp kullanmıyor|url)/.test(t) && t.length < 400) return t;
  return '';
};
W.composer = function(){
  const l = Array.from(document.querySelectorAll('footer [contenteditable="true"],#main footer [contenteditable]')).filter(vis);
  return l.length ? pos(l[0]) : null;
};
W.attach = function(){
  const c = Array.from(document.querySelectorAll('footer button,footer [role="button"],footer span[data-icon],footer div[data-icon]'));
  for (const e of c){ if (/(attach|ekle|plus|clip|paperclip|ek )/.test(lab(e)) && vis(e.closest('button,[role="button"]') || e)) return pos(e); }
  return null;
};
W.send = function(){
  const c = Array.from(document.querySelectorAll('[data-icon="send"],[aria-label="Send"],[aria-label="Gönder"],[data-testid="send"]')).filter(function(e){ return vis(e.closest('button,[role="button"]') || e); });
  return c.length ? pos(c[c.length - 1]) : null;
};
W.pending = function(){
  return document.querySelectorAll('[data-icon="msg-time"]').length + document.querySelectorAll('[data-icon="media-cancel"],[data-icon="x-viewer"]').length * 0;
};
W.sendOpen = function(){ return !!W.send(); };
window.__wa = W;
return true;
})()
"""


class WhatsApp:
    """WhatsApp Web'i ayrı bir tarayıcı penceresinde (kendi profiliyle) açıp dosya/mesaj gönderir."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.port = int(cfg.get("WHATSAPP_PORT", "9223"))
        self.url = cfg.get("WHATSAPP_URL", "https://web.whatsapp.com").rstrip("/")
        self.cdp = CDP(self.port, "whatsapp")
        self.lock = threading.Lock()
        self.profile = BASE / "whatsapp-profil"

    # --- kişiler
    def contacts(self):
        out = []
        for part in re.split(r"[;,]", self.cfg.get("WHATSAPP_KISILER", "")):
            if ":" in part:
                n, num = part.split(":", 1)
                num = re.sub(r"\D", "", num)
                if n.strip() and num:
                    out.append((n.strip(), num))
        me = re.sub(r"\D", "", self.cfg.get("WHATSAPP_NO", ""))
        if me and not any(n.lower() == "ben" for n, _ in out):
            out.insert(0, ("Ben (kendime)", me))
        return out

    def save_contacts(self, pairs):
        v = ",".join("%s:%s" % (n, num) for n, num in pairs if n != "Ben (kendime)")
        save_env_value("WHATSAPP_KISILER", v)
        self.cfg["WHATSAPP_KISILER"] = v

    # --- tarayıcı
    def find_browser(self):
        exe = self.cfg.get("WHATSAPP_TARAYICI") or os.environ.get("AGK_WA_EXE")
        if exe and Path(exe).exists():
            return exe
        env = os.environ
        for p in (env.get("ProgramFiles", "") + r"\Google\Chrome\Application\chrome.exe",
                  env.get("ProgramFiles(x86)", "") + r"\Google\Chrome\Application\chrome.exe",
                  env.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
                  env.get("ProgramFiles(x86)", "") + r"\Microsoft\Edge\Application\msedge.exe",
                  env.get("ProgramFiles", "") + r"\Microsoft\Edge\Application\msedge.exe"):
            if p and Path(p).exists():
                return p
        return None

    def ensure_browser(self):
        if self.cdp.available():
            return
        exe = self.find_browser()
        if not exe:
            raise UIError("Chrome ya da Edge bulunamadı. ayarlar.env içine WHATSAPP_TARAYICI=tam\\yol\\chrome.exe yaz.")
        self.profile.mkdir(exist_ok=True)
        args = [exe, "--remote-debugging-port=%d" % self.port, "--remote-allow-origins=*",
                "--user-data-dir=%s" % self.profile, "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-background-timer-throttling",
                "--no-first-run", "--no-default-browser-check", "--window-size=1100,800"]
        if os.environ.get("AGK_WA_HEADLESS"):
            args += ["--headless=new", "--no-sandbox"]
        args.append("--app=%s/" % self.url)
        subprocess.Popen(args)
        for _ in range(40):
            time.sleep(0.5)
            if self.cdp.available():
                return
        raise UIError("WhatsApp penceresi açılamadı.")

    def connect(self):
        self.ensure_browser()
        if not self.cdp.connected():
            self.cdp.connect()
        self.cdp.ev(WA_JS)

    def w(self, call):
        try:
            return self.cdp.ev("(function(){ if(!window.__wa){ return null; } return window.__wa." + call + "; })()")
        except CDPError:
            raise

    def wait(self, call, secs, want=True):
        end = time.time() + secs
        while time.time() < end:
            try:
                self.cdp.ev("window.__wa || " + json.dumps(None))
                if not self.cdp.ev("!!window.__wa"):
                    self.cdp.ev(WA_JS)
                v = self.w(call)
            except CDPError:
                v = None
            if bool(v) == want:
                return v
            time.sleep(0.6)
        return None

    def state(self):
        self.connect()
        for _ in range(25):
            if not self.cdp.ev("!!window.__wa"):
                self.cdp.ev(WA_JS)
            st = self.w("state()")
            if st in ("chats", "qr"):
                return st
            time.sleep(1)
        return "loading"

    def screenshot(self):
        self.connect()
        return self.cdp.screenshot()

    # --- gönderme
    def open_chat(self, number, text=""):
        from urllib.parse import quote
        self.connect()
        url = "%s/send?phone=%s" % (self.url, number) + (("&text=" + quote(text)) if text else "")
        self.cdp.send("Page.navigate", {"url": url})
        time.sleep(2.5)
        end = time.time() + 60
        while time.time() < end:
            if not self.cdp.connected():
                self.cdp.connect()
            try:
                if not self.cdp.ev("!!window.__wa"):
                    self.cdp.ev(WA_JS)
                if self.w("composer()"):
                    return
                err = self.w("error()")
                if err:
                    raise UIError("WhatsApp bu numarayı açamadı: %s" % short(err, 120))
                if self.w("state()") == "qr":
                    raise UIError("WhatsApp oturumu kapalı. Önce /whatsapp yazıp QR kodu okut.")
            except CDPError:
                pass
            time.sleep(1)
        raise UIError("Sohbet kutusu açılmadı (60 sn). WhatsApp Web yüklenmemiş olabilir.")

    def _click(self, call, secs, what):
        pos = self.wait(call, secs)
        if not pos:
            raise UIError("WhatsApp'ta '%s' bulunamadı." % what)
        self.cdp.click(pos["x"], pos["y"])

    def send_file(self, number, path, caption=""):
        p = Path(path)
        size = p.stat().st_size
        with self.lock:
            self.open_chat(number)
            self._click("attach()", 15, "ek (ataç) düğmesi")
            time.sleep(0.8)
            r = self.cdp.send("Runtime.evaluate", {"expression":
                "(function(){const l=Array.from(document.querySelectorAll('input[type=file]'));let b=null;"
                "for(const i of l){const a=i.getAttribute('accept')||'';if(a===''||a==='*'||a==='*/*')b=i;}"
                "return b||l[l.length-1]||null;})()"})
            obj = (r.get("result") or {}).get("objectId")
            if not obj:
                raise UIError("WhatsApp dosya seçme alanı bulunamadı.")
            self.cdp.send("DOM.setFileInputFiles", {"files": [str(p)], "objectId": obj})
            if not self.wait("send()", 40):
                raise UIError("Dosya önizlemesi açılmadı.")
            if caption:
                time.sleep(0.5)
                self.cdp.insert_text(caption)
            time.sleep(0.6)
            self._click("send()", 10, "gönder düğmesi")
            # gönderim bitene kadar bekle (saat simgeleri kaybolur, önizleme kapanır)
            end = time.time() + max(90, size / (1024 * 1024) * 4)
            time.sleep(2)
            while time.time() < end:
                try:
                    if not self.w("send()") and not self.w("pending()"):
                        return True
                except CDPError:
                    pass
                time.sleep(1.5)
        raise UIError("Gönderim süresi doldu; dosya yine de ulaşmış olabilir, WhatsApp'tan kontrol et.")

    def send_text(self, number, text):
        with self.lock:
            self.open_chat(number, text)
            time.sleep(0.8)
            pos = self.w("composer()")
            if pos:
                self.cdp.click(pos["x"], pos["y"])
            self.cdp.press_enter()
            end = time.time() + 30
            time.sleep(1.5)
            while time.time() < end:
                if not self.w("pending()"):
                    return True
                time.sleep(1)
        return True


class Bridge:
    ATT_STRATS = ("input", "drag", "paste", "drop")
    ATT_B64_MAX = 8 * 1024 * 1024

    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_API_BASE"])
        self.cdp = CDP(cfg["CDP_PORT"], cfg["UI_MARKERS"])
        self.lock = threading.RLock()      # Antigravity arayüzüne aynı anda tek işlem
        self.slock = threading.Lock()      # iç durum tabloları
        self.user_id = cfg["TELEGRAM_USER_ID"].strip()
        self.pair_code = "%06d" % random.randint(0, 999999)
        self.started = time.time()
        self.selected = None               # (proje, başlık)
        self.selected_at = 0
        self.pending = None                # sohbet seçilmeden yazılmış mesaj
        self.files = []                    # sıradaki dosyalar (bir sonraki mesajla gider)
        self.cap = None                    # dosyaya yazılan açıklama
        self.flush_timer = None
        self.gelen = Path(cfg.get("GELEN_KLASOR") or (BASE / "gelen"))
        self.convs = {}                    # id -> (proje, başlık)
        self.projs = {}                    # id -> proje
        self.rows = []                     # son tarama
        self.active = {}                   # çalışan konuşmalar
        self.finished = []                 # biten işler (en yeni başta)
        self.watch = {}                    # sohbet -> izleme durumu (onay/takılma)
        self.ap_cache = {}                 # sohbet id -> onay düğmesi adları
        self.fmap = {}                     # id -> bilgisayardaki dosya yolu
        self.newproj = None                # (proje, zaman): yeni sohbet modu
        self.danger = None                 # (işlem, zaman): onay bekleyen tehlikeli komut
        self.voice_pending = None
        self.stt_hint = False
        self._stt_model = None
        self.unauth_seen = {}
        self.sched = []
        self.sched_file = BASE / "zamanlanmis.json"
        self.down_since = None
        self.restarts = []
        self.warned_noport = False
        self.last_visit = 0
        self.last_full = 0
        self.last_diag = 0
        self.visit_i = 0
        self.cdp_ok = None
        self.keep_awake_on = False
        self.poll = float(cfg["POLL_SECONDS"])
        self.confirm = int(cfg["DONE_CONFIRM"])
        self.auto_answer = cfg.get("AUTO_ANSWER", "0") == "1"
        self.header_check = cfg.get("HEADER_CHECK", "1") != "0"
        self.safe_mode = cfg.get("GUVENLI_MOD", "1") != "0"
        self.idle_visit = float(cfg.get("IDLE_VISIT", "300"))
        self.visit_every = float(cfg.get("VISIT_EVERY", "60"))
        self.stuck_min = float(cfg.get("STUCK_MIN", "15"))
        self.long_min = float(cfg.get("LONG_MIN", "60"))
        roots = [x.strip() for x in re.split(r"[;|]", cfg.get("DOSYA_KOKLERI", "")) if x.strip()]
        self.roots = roots or [str(Path.home())]
        self.roots.append(str(self.gelen))
        self.wa = WhatsApp(cfg)
        self.proj_used = {}
        self.model_opts = {}
        self.digest = []
        self.update_pending = None
        self.last_scan = 0
        self.sched_load()
        self.fin_file = BASE / "bitenler.json"
        self.finished_load()
        self.dp = None
        self.dp_busy = False
        self.fin_sig = {}
        self.last_notif = {}
        self.muted = self.mute_load()
        self.pin_until = 0
        self.pin_wait = None
        self.pin_fail = []
        self.cur_msg = None
        self.dy_pending = None
        self.ozet_date = ""

    def finished_load(self):
        try:
            d = json.loads(self.fin_file.read_text(encoding="utf-8"))
            self.finished = [e for e in d if isinstance(e, dict) and "project" in e and "title" in e][:30]
        except Exception:
            self.finished = []

    def finished_save(self):
        try:
            self.fin_file.write_text(json.dumps(self.finished[:30], ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ bağlantı
    def with_cdp(self, fn):
        with self.lock:
            last = None
            for _ in range(2):
                try:
                    if not self.cdp.connected():
                        self.cdp.connect()
                    return fn()
                except CDPError as e:
                    last = e
                    self.cdp.close()
            raise last

    def say(self, text, markup=None):
        if self.user_id:
            self.tg.send_text(self.user_id, text, markup)

    # ------------------------------------------------------------------ Antigravity işlemleri
    def snapshot(self, full=False):
        """Sol listeyi okur. full=True: listeyi baştan sona kaydırarak gizli kalan sohbetleri de bulur."""
        def f():
            self.cdp.agk("expandMore()") if full else None
            info = self.cdp.agk("sbInfo()") if full else None
            if not info or info["sh"] <= info["ch"] + 20:
                return self.cdp.agk("rows()") or []
            seen, out, y = set(), [], 0
            known = {}   # sohbet başlığı -> proje (önceki parçalardan)
            last_folder = ""
            step = max(120, int(info["ch"] * 0.8))
            top0 = info["top"]
            for _ in range(60):
                pos = self.cdp.agk("sbTo(%d)" % y)
                time.sleep(0.3)
                rows = list(self.cdp.agk("rows()") or [])
                time.sleep(0.25)
                more = self.cdp.agk("rows()") or []
                have = {(r["kind"], r["title"]) for r in rows}
                rows += [r for r in more if (r["kind"], r["title"]) not in have]
                cur = ""
                for r in rows:  # listenin üstünde kalan klasör başlığı sanallaştırmada görünmeyebilir
                    if r["kind"] == "folder":
                        cur = r["project"]
                        break
                    if r["project"] == "":
                        r["project"] = known.get(r["title"], last_folder)
                for r in rows:
                    if r["kind"] == "folder":
                        last_folder = r["project"]
                    elif r["project"] == "":
                        r["project"] = last_folder
                    k = (r["kind"], r["project"], r["title"])
                    if r["kind"] == "conv":
                        known.setdefault(r["title"], r["project"])
                    if k not in seen:
                        seen.add(k)
                        out.append(r)
                if not pos or y + info["ch"] >= pos["sh"] - 2:
                    break
                y += step
            self.cdp.agk("sbTo(%d)" % top0)
            time.sleep(0.2)
            self.cdp.agk("sbTo(%d)" % top0)
            return out
        rows = self.with_cdp(f) or []
        with self.slock:
            self.rows = rows
            for r in rows:
                if r["kind"] == "folder":
                    self.projs[hid(r["project"])] = r["project"]
                else:
                    self.convs[hid(r["project"], r["title"])] = (r["project"], r["title"])
        return rows

    def list_convs(self, full=False):
        return [r for r in self.snapshot(full) if r["kind"] == "conv"]

    def find_row(self, project, title):
        """Sohbet satırını bulur; görünmüyorsa listeyi kaydırarak arar."""
        q = "target(%s, %s)" % (json.dumps(project), json.dumps(title))
        pos = self.cdp.agk(q)
        if pos:
            return pos
        self.cdp.agk("expandMore()")
        info = self.cdp.agk("sbInfo()")
        if not info:
            return None
        top0, y = info["top"], 0
        step = max(120, int(info["ch"] * 0.8))
        for _ in range(60):
            cur = self.cdp.agk("sbTo(%d)" % y)
            for _ in range(3):
                time.sleep(0.25)
                pos = self.cdp.agk(q)
                if pos:
                    return pos
            if not cur or y + info["ch"] >= cur["sh"] - 2:
                break
            y += step
        self.cdp.agk("sbTo(%d)" % top0)
        return None

    def open_conv(self, project, title):
        """Sohbeti açar ve DOĞRU sohbetin açıldığını başlıktan doğrular."""
        def f():
            for _ in range(2):
                pos = self.find_row(project, title)
                if not pos:
                    raise UIError("Sohbet sol listede görünmüyor: %s" % title)
                self.cdp.click(pos["x"], pos["y"])
                time.sleep(1.4)
                if not self.header_check:
                    return
                hdr = self.cdp.agk("headerText()") or ""
                if header_matches(hdr, project, title):
                    return
            raise UIError("Doğru sohbetin açıldığını doğrulayamadım. Hiçbir şey yazmadım.")
        self.with_cdp(f)

    def current_open(self):
        """Şu an ekranda açık olan sohbet (proje, başlık) ya da None."""
        hdr = self.with_cdp(lambda: self.cdp.agk("headerText()")) or ""
        for r in self.rows:
            if r["kind"] == "conv" and header_matches(hdr, r["project"], r["title"]):
                return (r["project"], r["title"])
        return None

    def read_raw(self):
        def f():
            last = None
            try:
                for _ in range(6):
                    r = self.cdp.agk("readPane(true)") or {}
                    t = r.get("text", "")
                    if t and t == last:
                        break
                    last = t
                    time.sleep(0.8)
            finally:
                try:
                    self.cdp.agk("restorePane()")   # sohbeti kullanıcının bıraktığı yere geri kaydır
                except Exception:
                    pass
            return last or ""
        return self.with_cdp(f)

    def read_quick(self):
        r = self.with_cdp(lambda: self.cdp.agk("readPane(false)")) or {}
        return r.get("text", "")

    def read_answer(self):
        return extract_final(self.read_raw())

    def read_tail(self, n=3500):
        return format_activity(self.read_raw())[-n:]

    def send_to_antigravity(self, text, clear=True):
        def f():
            c = self.cdp.agk("prepComposer(%s)" % ("true" if clear else "false"))
            if not c:
                raise UIError("Mesaj kutusu bulunamadı")
            self.cdp.click(c["x"], c["y"])
            time.sleep(0.2)
            self.cdp.insert_text(text)
            time.sleep(0.4)
            now = self.cdp.agk("composerText()") or ""
            probe = re.sub(r"\s+", " ", text.strip())[:12]
            if probe and probe not in re.sub(r"\s+", " ", now):
                raise UIError("Yazı mesaj kutusuna girmedi")
            self.cdp.press_enter()
            time.sleep(1.2)
        self.with_cdp(f)

    # --- dosyaları gerçek ek olarak ekleme
    def _att_state(self):
        return self.cdp.agk("attState()") or {"n": 0, "h": 0}

    def _att_input(self, path, pos):
        return self.cdp.set_input_files([path])

    def _att_drag(self, path, pos):
        self.cdp.agk("guard(true)")
        self.cdp.drag_files([path], pos["x"], pos["y"])
        return True

    def _att_blob(self, path):
        pth = Path(path)
        if pth.stat().st_size > self.ATT_B64_MAX:
            return None
        mime = mimetypes.guess_type(pth.name)[0] or "application/octet-stream"
        return json.dumps(pth.name), json.dumps(mime), json.dumps(base64.b64encode(pth.read_bytes()).decode("ascii"))

    def _att_paste(self, path, pos):
        b = self._att_blob(path)
        return bool(b) and bool(self.cdp.agk("synthPaste(%s,%s,%s)" % b))

    def _att_drop(self, path, pos):
        b = self._att_blob(path)
        return bool(b) and bool(self.cdp.agk("synthDrop(%s,%s,%s)" % b))

    def attach_one(self, path):
        """Dosyayı gerçek ek olarak eklemeyi sırayla dener; çalışan yöntemin adını döndürür."""
        order = list(self.ATT_STRATS)
        pref = getattr(self, "att_pref", None)
        if pref in order:
            order.remove(pref)
            order.insert(0, pref)
        try:
            for name in order:
                pos = self.cdp.agk("prepComposer(false)")
                if not pos:
                    raise UIError("Mesaj kutusu bulunamadı")
                before = self._att_state()
                try:
                    if not getattr(self, "_att_" + name)(path, pos):
                        continue
                except CDPError as e:
                    log("Ek yöntemi '%s' hata verdi: %s" % (name, e))
                    if not self.cdp.connected():
                        raise
                    continue
                for _ in range(5):
                    time.sleep(0.5)
                    after = self._att_state()
                    if after["n"] > before["n"] or after["h"] > before["h"] + 80:
                        self.att_pref = name
                        log("Dosya ek olarak eklendi (%s): %s" % (name, Path(path).name))
                        return name
                log("Ek yöntemi '%s' etki etmedi" % name)
        finally:
            try:
                self.cdp.agk("guard(false)")
            except CDPError:
                pass
        return None

    def _send_into_open(self, text, paths=()):
        """Açık sohbete (dosyaları ek olarak ekleyip) mesajı yazar ve gönderir."""
        ok, bad = [], list(paths)
        if paths and self.cfg.get("ATTACH_MODE", "auto") != "path":
            bad = []
            for pth in paths:
                if self.with_cdp(lambda pth=pth: self.attach_one(pth)):
                    ok.append(pth)
                else:
                    bad.append(pth)
        body = (text or "").strip()
        if paths and not body:
            body = "Ekli dosyaları incele." if ok and not bad else "Telegram'dan gelen dosyaları incele."
        if bad:
            body = "%s (Ekli dosyalar: %s)" % (body, ", ".join('"%s"' % x for x in bad))
        self.send_to_antigravity(body, clear=not ok)
        log("GÖNDERİLDİ: %s" % short(body, 120))
        return {"ok": len(ok), "bad": len(bad)}

    def write_to(self, project, title, text, paths=()):
        """Sohbete yazar. Dosyaları önce gerçek ek olarak eklemeyi dener, olmazsa yolunu yazar."""
        with self.lock:
            self.open_conv(project, title)
            res = self._send_into_open(text, paths)
        with self.slock:
            self.active[project + "||" + title] = {
                "miss": 0, "gone": 0, "start": time.time(), "seen": False,
                "deadline": time.time() + 25, "sent": True,
                "info": {"project": project, "title": title, "running": True, "age": "", "kind": "conv"}}
        return res

    def open_new(self, project):
        """Projede yeni sohbet açar; açılan sayfanın o projede olduğunu doğrular."""
        def f():
            clicked = False
            if project == NEW_GENERAL:
                if self.cdp.agk("navNewClick()"):
                    time.sleep(1.5)
                    return
                nav = self.cdp.agk("navNewPos()")
                if not nav:
                    raise UIError("Soldaki 'New Conversation' düğmesini bulamadım")
                self.cdp.click(nav["x"], nav["y"])
                time.sleep(1.5)
                return
            if self.cdp.agk("newConvClick(%s)" % json.dumps(project)):
                clicked = True
                time.sleep(1.5)
            pos = None if clicked else self.cdp.agk("folderPos(%s)" % json.dumps(project))
            if pos:
                self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": pos["x"], "y": pos["y"]})
                time.sleep(0.4)
                plus = self.cdp.agk("newConvPos(%s)" % json.dumps(project))
                if plus:
                    self.cdp.click(plus["x"], plus["y"])
                    clicked = True
                    time.sleep(1.5)
            if not clicked:
                nav = self.cdp.agk("navNewPos()")
                if not nav:
                    raise UIError("Yeni sohbet düğmesini bulamadım")
                self.cdp.click(nav["x"], nav["y"])
                time.sleep(1.5)
            if self.cfg.get("NEW_CONV_RELAXED") != "1":
                hdr = self.cdp.agk("headerText()") or ""
                if norm(project) not in norm(hdr):
                    raise UIError("Yeni sohbetin '%s' projesinde açıldığını doğrulayamadım. Hiçbir şey yazmadım. "
                                   "(Her seferinde bu çıkarsa ayarlar.env içine NEW_CONV_RELAXED=1 yazılabilir.)" % project)
        self.with_cdp(f)

    def start_new(self, project, text, paths=()):
        if project == WIZ_MARK:
            newdir = self.apply_wizard()
            if newdir:
                text = ("Bu iş için yeni bir proje klasörü kullan: %s\n"
                        "Klasör yoksa önce oluştur, bu işle ilgili tüm dosyaları ve kodu bu klasörün içine yaz.\n\n"
                        "İstek: %s") % (newdir, text)
        with self.lock:
            if project == WIZ_MARK:
                pass
            else:
                self.open_new(project)
            return self._send_into_open(text, paths)

    # ------------------------------------------------------------------ menüler
    def selected_valid(self):
        if self.selected and time.time() - self.selected_at < SELECT_TTL:
            return self.selected
        return None

    def select(self, pc):
        self.selected = pc
        self.selected_at = time.time()
        self.proj_used[pc[0]] = time.time()

    def newproj_valid(self):
        if self.newproj and time.time() - self.newproj[1] < NEW_TTL:
            return self.newproj[0]
        self.newproj = None
        return None

    def row_of(self, project, title):
        for r in self.rows:
            if r["kind"] == "conv" and r["project"] == project and r["title"] == title:
                return r
        return None

    def projects_menu(self, note="", page=0):
        """Projeleri en son kullanılandan geriye doğru sıralar; sayfa başına PROJE_SAYFA (5) proje gösterir."""
        rows = self.snapshot(full=True)
        order, data = [], {}
        now = time.time()
        for r in rows:
            p = r["project"]
            if p not in data:
                data[p] = {"n": 0, "run": 0, "age": None}
                order.append(p)
            if r["kind"] == "conv":
                d = data[p]
                d["n"] += 1
                if r["running"]:
                    d["run"] += 1
                    d["age"] = 0
                else:
                    a = age_seconds(r.get("age"))
                    if a is not None and (d["age"] is None or a < d["age"]):
                        d["age"] = a
        if not order:
            return ("Sol listede proje göremedim. Antigravity penceresi açık ve küçültülmemiş mi? (/teshis)", None)
        for p in order:  # botla yakın zamanda açılan proje de "yeni" sayılır
            used = self.proj_used.get(p)
            if used:
                u = int(now - used)
                data[p]["age"] = u if data[p]["age"] is None else min(data[p]["age"], u)
        idx = {p: i for i, p in enumerate(order)}
        empties = [p for p in order if data[p]["n"] == 0]
        if len(empties) < len(order):
            order = [p for p in order if data[p]["n"] > 0]  # içinde sohbet olmayan klasörleri ana listede gösterme
        else:
            empties = []
        ranked = sorted(order, key=lambda p: (data[p]["age"] is None, data[p]["age"] or 0, idx[p]))
        size = max(3, int(self.cfg.get("PROJE_SAYFA", "5")))
        pages = max(1, (len(ranked) + size - 1) // size)
        page = max(0, min(page, pages - 1))
        kb = []
        for p in ranked[page * size:(page + 1) * size]:
            d = data[p]
            t = "📁 %s · %d sohbet" % (short(disp_name(p), 24), d["n"])
            if d["run"]:
                t += " · ⏳%d" % d["run"]
            elif d["age"] is not None:
                t += " · " + fmt_ago(d["age"])
            kb.append([{"text": t, "callback_data": "p:" + hid(p)}])
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Daha yeniler", "callback_data": "pg:%d" % (page - 1)})
        left = len(ranked) - (page + 1) * size
        if left > 0:
            nav.append({"text": "➕ Daha fazlası (%d)" % left, "callback_data": "pg:%d" % (page + 1)})
        if nav:
            kb.append(nav)
        kb.append([{"text": "➕ Yeni sohbet (soldaki New Conversation)", "callback_data": "nn"}])
        if empties and page == pages - 1:
            kb.append([{"text": "📂 Boş görünen klasörler (%d)" % len(empties), "callback_data": "pe"}])
        kb.append([{"text": "🔄 Yenile", "callback_data": "pg:%d" % page}])
        text = (note + "\n\n" if note else "") + "📁 Projeler\n" + LINE + "\n🕒 En son kullanılandan başlayarak"
        if pages > 1:
            text += "\nSayfa %d/%d · toplam %d proje" % (page + 1, pages, len(ranked))
        sel = self.selected_valid()
        if sel:
            text += "\n\n👉 Seçili sohbet: %s / %s" % sel
        return text, kb

    def empties_menu(self):
        rows = self.snapshot(full=True)
        cnt = {}
        for r in rows:
            cnt[r["project"]] = cnt.get(r["project"], 0) + (1 if r["kind"] == "conv" else 0)
        kb = [[{"text": "📂 %s" % short(disp_name(p), 40), "callback_data": "p:" + hid(p)}] for p, n in cnt.items() if n == 0][:15]
        kb.append([{"text": "⬅️ Projeler", "callback_data": "home"}])
        return "📂 İçinde sohbet görünmeyen klasörler (boş ya da kapalı olabilir).\nDokunursan açıp bakarım:", kb

    def convs_menu(self, pid, page=0):
        project = self.projs.get(pid)
        if project is None:
            return self.projects_menu("Liste yenilendi.")
        self.proj_used[project] = time.time()
        rows = self.snapshot(full=True)
        convs = [r for r in rows if r["kind"] == "conv" and r["project"] == project]
        if not convs:
            # klasör kapalı olabilir: bir kez açıp tekrar bak
            try:
                with self.lock:
                    def tog():
                        pos = self.cdp.agk("folderPos(%s)" % json.dumps(project))
                        if pos:
                            self.cdp.click(pos["x"], pos["y"])
                            time.sleep(0.8)
                    self.with_cdp(tog)
                rows = self.snapshot(full=True)
                convs = [r for r in rows if r["kind"] == "conv" and r["project"] == project]
            except CDPError:
                pass
        idx = {id(r): i for i, r in enumerate(convs)}
        convs = sorted(convs, key=lambda r: (0 if r["running"] else 1,
                                              age_seconds(r.get("age")) if age_seconds(r.get("age")) is not None else 10 ** 12,
                                              idx[id(r)]))
        size = max(3, int(self.cfg.get("SOHBET_SAYFA", "6")))
        pages = max(1, (len(convs) + size - 1) // size)
        page = max(0, min(page, pages - 1))
        kb = []
        for r in convs[page * size:(page + 1) * size]:
            mark = "⏳" if r["running"] else "💬"
            t = "%s %s" % (mark, short(r["title"], 36))
            if r.get("age") and not r["running"]:
                t += " · " + r["age"]
            kb.append([{"text": t, "callback_data": "c:" + hid(r["project"], r["title"])}])
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Daha yeniler", "callback_data": "cg:%s:%d" % (pid, page - 1)})
        left = len(convs) - (page + 1) * size
        if left > 0:
            nav.append({"text": "➕ Daha fazlası (%d)" % left, "callback_data": "cg:%s:%d" % (pid, page + 1)})
        if nav:
            kb.append(nav)
        kb.append([{"text": "➕ Yeni sohbet", "callback_data": "n:" + pid}])
        kb.append([{"text": "⬅️ Projeler", "callback_data": "home"}])
        return "📁 %s\nBir sohbet seç (en yeni üstte):" % disp_name(project), kb

    def conv_panel(self, cid):
        pc = self.convs.get(cid)
        if not pc:
            return self.projects_menu("Bu sohbet artık listede yok. Liste yenilendi.")
        self.snapshot(full=True)
        pc = self.convs.get(cid)
        r = self.row_of(*pc)
        if not r:
            return self.projects_menu("Bu sohbet artık listede yok. Liste yenilendi.")
        self.select(pc)
        self.newproj = None
        status = "⏳ çalışıyor" if r["running"] else "💤 boşta"
        text = "💬 %s\n📁 %s\nDurum: %s\n\n👉 Seçildi. Mesajını yaz, bu sohbete göndereyim." % (pc[1], pc[0], status)
        kb = []
        if self.pending or self.files:
            if self.pending:
                text += "\n\n📝 Bekleyen mesajın:\n«%s»" % short(self.pending, 300)
            if self.files:
                text += "\n\n📎 %d dosya eklenecek" % len(self.files)
            kb.append([{"text": "📤 Gönder", "callback_data": "send:" + cid},
                       {"text": "✖️ İptal", "callback_data": "cancel"}])
        kb.append([{"text": "📥 Son cevap", "callback_data": "a:" + cid},
                   {"text": "📜 Son işlemler", "callback_data": "l:" + cid}])
        kb.append([{"text": "📂 Dosya getir", "callback_data": "fl:" + cid},
                   {"text": "🛑 Durdur", "callback_data": "s:" + cid}])
        kb.append([{"text": "🤖 Model", "callback_data": "md:" + cid},
                   {"text": "🔁 Tekrar dene", "callback_data": "rt:" + cid}])
        kb.append([{"text": "⬅️ Sohbetler", "callback_data": "p:" + hid(pc[0])}])
        return text, kb

    def finished_menu(self):
        if not self.finished:
            return "Henüz biten iş kaydı yok. (Köprü açıldığından beri biten işler burada listelenir.)", None
        kb, lines = [], ["✅ Biten işler (en yeni üstte):"]
        for e in self.finished[:12]:
            cid = hid(e["project"], e["title"])
            self.convs[cid] = (e["project"], e["title"])
            hh = time.strftime("%H:%M", time.localtime(e["ts"]))
            kb.append([{"text": "📥 %s · %s · %s" % (hh, short(e["project"], 12), short(e["title"], 28)),
                        "callback_data": "a:" + cid}])
        return "\n".join(lines) + "\nCevabını çekmek istediğine dokun:", kb

    def running_menu(self):
        convs = [r for r in self.list_convs() if r["running"]]
        if not convs:
            return "Şu an çalışan iş yok. 💤", None
        kb = []
        for r in convs:
            cid = hid(r["project"], r["title"])
            kb.append([{"text": "⏳ %s / %s" % (short(r["project"], 14), short(r["title"], 30)),
                        "callback_data": "c:" + cid}])
        return "⏳ Çalışan işler (%d):" % len(convs), kb

    # ------------------------------------------------------------------ dosya alma (bilgisayardan Telegram'a)
    def reg_file(self, path):
        fid = hid(str(path))
        self.fmap[fid] = str(path)
        if len(self.fmap) > 300:
            for k in list(self.fmap)[:100]:
                del self.fmap[k]
        return fid

    def existing_files(self, text, limit=4):
        out = []
        for pth in find_paths(text):
            try:
                p = Path(pth)
                if p.is_file() and path_allowed(p, self.roots):
                    out.append(p)
            except OSError:
                pass
            if len(out) >= limit:
                break
        return out

    def file_buttons(self, files):
        kb = []
        for p in files:
            fid = self.reg_file(p)
            size = p.stat().st_size
            if size > MAX_SEND:
                kb.append([{"text": "📲 WhatsApp'a gönder · %s · %s" % (short(p.name, 22), human_size(size)), "callback_data": "wa:" + fid}])
            else:
                kb.append([{"text": "📤 %s · %s" % (short(p.name, 22), human_size(size)), "callback_data": "f:" + fid},
                           {"text": "📲 WhatsApp", "callback_data": "wa:" + fid}])
        return kb

    # --- WhatsApp
    def wa_pick(self, chat, path):
        p = Path(path)
        if not p.is_file() or not path_allowed(p, self.roots):
            self.tg.send_text(chat, "⚠️ Bu dosyayı gönderemem (yok ya da izinli klasörlerin dışında/korumalı).")
            return
        cts = self.wa.contacts()
        if not cts:
            self.tg.send_text(chat, "WhatsApp'ta kime gideceğini bilmiyorum. Önce kişi ekle:\n"
                                    "/wakisi ben 905551112233   (kendi numaran)\n/wakisi ali 905441112233\n"
                                    "(Numara ülke koduyla, başında + ve boşluk olmadan.)")
            return
        fid = self.reg_file(p)
        kb = [[{"text": "👤 %s" % n, "callback_data": "wc:%s:%d" % (fid, i)}] for i, (n, _) in enumerate(cts)]
        kb.append([{"text": "✖️ Vazgeç", "callback_data": "dno"}])
        self.tg.send_text(chat, "📲 %s (%s)\nKime gönderilsin?" % (p.name, human_size(p.stat().st_size)), kb)

    def wa_send_async(self, chat, path, idx):
        cts = self.wa.contacts()
        p = Path(path)
        if idx >= len(cts) or not p.is_file() or not path_allowed(p, self.roots):
            self.tg.send_text(chat, "Bu seçimin süresi dolmuş. Tekrar dene.")
            return
        name, num = cts[idx]
        send_p = p
        if p.suffix.lower() in WA_BLOCKED_EXT:
            import zipfile
            send_p = self.gelen / (p.stem + ".zip")
            self.gelen.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(send_p), "w", zipfile.ZIP_DEFLATED) as z:
                z.write(str(p), p.name)
        self.tg.send_text(chat, "⏳ %s → %s WhatsApp'a gönderiliyor (%s). Bilgisayarda bir WhatsApp penceresi açılabilir, kapatma."
                          % (p.name, name, human_size(send_p.stat().st_size)))

        def work():
            try:
                self.wa.send_file(num, send_p)
                self.tg.send_text(chat, "✅ WhatsApp'a gönderildi\n👤 %s\n📎 %s" % (name, send_p.name))
                log("WhatsApp'a gönderildi: %s -> %s" % (send_p.name, name))
            except (UIError, CDPError) as e:
                self.tg.send_text(chat, "⚠️ WhatsApp gönderimi olmadı: %s" % e)
                try:
                    self.tg.send_photo(chat, "whatsapp.png", self.wa.screenshot(), "WhatsApp penceresinin son hali")
                except Exception:
                    pass
            except Exception as e:
                log("WhatsApp hatası:\n" + traceback.format_exc())
                self.tg.send_text(chat, "⚠️ WhatsApp hatası: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def cmd_whatsapp(self, chat):
        def work():
            try:
                st = self.wa.state()
                if st == "chats":
                    cts = self.wa.contacts()
                    self.tg.send_text(chat, "✅ WhatsApp bağlı.\nKişiler: %s\n\nDosya göndermek için 📲 WhatsApp düğmesi ya da /wa <dosya yolu>.\n"
                                            "Kişi eklemek: /wakisi ad numara" % (", ".join(n for n, _ in cts) or "yok (önce /wakisi ekle)"))
                elif st == "qr":
                    self.tg.send_photo(chat, "qr.png", self.wa.screenshot(),
                                       "QR kodu telefonundan okut: WhatsApp → Ayarlar → Bağlı cihazlar → Cihaz bağla. Sonra tekrar /whatsapp yaz.")
                else:
                    self.tg.send_text(chat, "WhatsApp Web henüz yüklenmedi, biraz sonra tekrar /whatsapp yaz.")
            except (UIError, CDPError) as e:
                self.tg.send_text(chat, "⚠️ %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def cmd_wakisi(self, chat, arg):
        a = (arg or "").split()
        cts = [(n, num) for n, num in self.wa.contacts() if n != "Ben (kendime)"]
        if not a:
            lines = ["👤 WhatsApp kişileri:"] + ["• %s: %s" % (n, num) for n, num in self.wa.contacts()]
            self.tg.send_text(chat, "\n".join(lines if len(lines) > 1 else ["Kişi yok."]) +
                              "\n\nEkle: /wakisi ali 905441112233\nSil: /wakisi sil ali")
        elif a[0].lower() == "sil" and len(a) > 1:
            cts = [(n, num) for n, num in cts if n.lower() != a[1].lower()]
            self.wa.save_contacts(cts)
            self.tg.send_text(chat, "Silindi: %s" % a[1])
        elif len(a) >= 2 and re.sub(r"\D", "", a[-1]):
            name, num = " ".join(a[:-1]), re.sub(r"\D", "", a[-1])
            if len(num) < 10:
                self.tg.send_text(chat, "Numara kısa görünüyor. Ülke koduyla yaz: 905551112233")
                return
            cts = [(n, x) for n, x in cts if n.lower() != name.lower()] + [(name, num)]
            if name.lower() == "ben":
                self.cfg["WHATSAPP_NO"] = num
                save_env_value("WHATSAPP_NO", num)
                cts = [(n, x) for n, x in cts if n.lower() != "ben"]
            self.wa.save_contacts(cts)
            self.tg.send_text(chat, "✅ Eklendi: %s (%s)" % (name, num))
        else:
            self.tg.send_text(chat, "Kullanım: /wakisi ali 905441112233")

    def send_pc_file_hint(self, chat, p):
        pass

    def send_pc_file(self, chat, path):
        p = Path(path)
        if not p.is_file() or not path_allowed(p, self.roots):
            self.tg.send_text(chat, "⚠️ Bu dosyayı gönderemem (yok ya da izinli klasörlerin dışında/korumalı).")
            return
        size = p.stat().st_size
        if size > MAX_SEND:
            self.tg.send_text(chat, "📦 %s çok büyük (%s); Telegram botları en fazla 50 MB gönderebilir.\n"
                                    "WhatsApp'a gönderebilirim:" % (p.name, human_size(size)),
                              [[{"text": "📲 WhatsApp'a gönder", "callback_data": "wa:" + self.reg_file(p)}]])
            return
        data = p.read_bytes()
        cap = "%s · %s" % (p.name, human_size(size))
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and size <= 10 * 1024 * 1024:
            self.tg.send_photo(chat, p.name, data, cap)
        else:
            self.tg.send_document(chat, p.name, data, cap)
        log("Dosya Telegram'a gönderildi: %s" % p.name)

    def cmd_dosya(self, chat, arg):
        arg = (arg or "").strip().strip('"')
        if not arg:
            self.tg.send_text(chat, "Kullanım:\n/dosya C:\\yol\\dosya.apk  → dosyayı gönderir\n"
                                    "/dosya C:\\yol\\klasor  → klasördeki en yeni 10 dosyayı listeler\n"
                                    "(İzinli klasörler: %s)" % ", ".join(self.roots[:-1]))
            return
        p = Path(arg)
        if p.is_file():
            self.send_pc_file(chat, p)
        elif p.is_dir():
            if not path_allowed(p, self.roots):
                self.tg.send_text(chat, "⚠️ Bu klasör izinli klasörlerin dışında.")
                return
            fl = newest_files(p)
            if not fl:
                self.tg.send_text(chat, "Klasörde dosya bulamadım.")
                return
            kb = self.file_buttons(fl)
            self.tg.send_text(chat, "📂 %s\nEn yeni %d dosya:" % (p, len(fl)), kb)
        else:
            self.tg.send_text(chat, "Bu yolu bulamadım: %s" % arg)

    # ------------------------------------------------------------------ bitiş bildirimi, takip
    def user_away(self):
        """Kullanıcı bilgisayardan uzakta mı? Antigravity ön plandaysa (okuyor/yazıyor) 15 dk ekstra bekler."""
        idle = idle_seconds()
        need = self.idle_visit + (900 if fg_is_antigravity() else 0)
        return idle >= need

    def finish_summary(self, project, title):
        with self.lock:
            cur = self.current_open()
            if cur != (project, title):
                if not self.user_away():
                    return None  # sen bilgisayardasın, ekranı değiştirmem
                self.open_conv(project, title)
            raw = self.read_raw()
        lines, ans = summarize(raw)
        return {"lines": lines, "files": self.existing_files(ans), "limit": bool(LIMIT_RE.search(raw[-700:]))}

    def finish(self, key, st):
        project, title = key.split("||", 1)
        dur = time.time() - st.get("start", time.time())
        cid = hid(project, title)
        with self.slock:
            self.convs[cid] = (project, title)
            self.finished = [e for e in self.finished if (e["project"], e["title"]) != (project, title)]
            self.finished.insert(0, {"project": project, "title": title, "ts": time.time(), "dur": dur})
            del self.finished[30:]
            self.watch.pop(key, None)
        self.finished_save()
        # Aynı bitişi tekrar bildirme: sohbetin son yazısı (rakamsız) öncekiyle aynıysa ve ben mesaj göndermediysem sessiz geç
        try:
            if self.current_open() == (project, title):
                raw = re.sub(r"\d+", "", (self.read_quick() or "")[-1500:])
                sig = hashlib.md5(raw.encode("utf-8")).hexdigest()
                prev = self.fin_sig.get(key)
                sent_after = st.get("sent", False)
                self.fin_sig[key] = sig
                if prev == sig and not sent_after:
                    log("Tekrar bildirim engellendi (sohbet değişmedi, kaynak: %s): %s" % (st.get("why", "?"), key))
                    return
        except Exception:
            pass
        sent = st.get("sent", False)
        why_skip = None
        if key in self.muted:
            why_skip = "sohbet susturulmuş"
        elif not self.notif_on():
            why_skip = "bildirimler kapalı"
        elif not sent and dur < self.notif_min():
            why_skip = "kısa iş (%d sn)" % dur
        elif not sent and dur < 120 and time.time() - self.last_notif.get(key, 0) < self.notif_gap() * 60:
            why_skip = "aynı sohbetten az önce bildirildi"
        if why_skip:
            log("Bildirim atlandı (%s): %s" % (why_skip, key))
            return
        self.last_notif[key] = time.time()
        if self.quiet_now():
            self.digest.append((project, title, time.time()))
            log("Sessiz saat: bildirim biriktirildi: %s / %s" % (project, title))
            return
        text = "✅ Bot işlemi bitirdi\n\n📁 Proje: %s\n💬 Sohbet: %s\n⏱ Süre: %s  •  🕐 %s" % (
            project or "(projesiz)", title, fmt_dur(dur), time.strftime("%H:%M"))
        extra_kb = []
        limit = False
        if self.cfg.get("OZET", "1") != "0":
            try:
                info = self.finish_summary(project, title)
            except Exception as e:
                log("Özet alınamadı: %s" % e)
                info = None
            if info:
                if info["lines"]:
                    text += "\n\n" + "\n".join(info["lines"])
                extra_kb = self.file_buttons(info["files"])
                limit = info.get("limit")
        if limit:
            extra_kb.append([{"text": "🔁 Tekrar dene", "callback_data": "rt:" + cid},
                             {"text": "⏰ 1 saat sonra devam et", "callback_data": "lz:%s:60" % cid}])
        if not st.get("seen"):
            text += "\n\n(Çalışma göstergesini görmedim; cevabı kontrol et.)"
        kb = [[{"text": "📥 Cevabı getir", "callback_data": "a:" + cid},
               {"text": "✍️ Yaz", "callback_data": "c:" + cid}]] + extra_kb
        kb.append([{"text": "🔕 Bu sohbeti sustur", "callback_data": "mu:" + cid}])
        self.say(text, kb)
        log("Bitti: %s / %s" % (project, title))
        if self.auto_answer:
            try:
                self.send_answer(self.user_id, cid)
            except Exception as e:
                log("Otomatik cevap alınamadı: %s" % e)

    def scan_once(self):
        now = time.time()
        full = self.user_away() and now - self.last_full >= float(self.cfg.get("FULL_SCAN_SEC", "20"))
        if full:
            self.last_full = now
        convs = self.list_convs(full)
        if not convs:
            return
        present = {conv_key(r) for r in convs}
        running = {conv_key(r): r for r in convs if r.get("running")}
        # açık olan sohbette durdur düğmesi / "Working" varsa çalışıyordur (kenar çubuğu göstergesi kaçmış olabilir)
        try:
            opc = self.current_open()
            if opc:
                ok = opc[0] + "||" + opc[1]
                why = ""
                if ok not in running:
                    why = self.with_cdp(lambda: self.cdp.agk("busyWhy()")) or ""
                if why:
                    row = self.row_of(*opc)
                    if row:
                        running[ok] = dict(row, running=True, why=why)
                        present.add(ok)
        except CDPError:
            raise
        except Exception:
            pass
        done = []
        with self.slock:
            for k, r in running.items():
                st = self.active.get(k)
                if st:
                    st.update(miss=0, gone=0, info=r, seen=True, last_run=now)
                else:
                    self.active[k] = {"miss": 0, "gone": 0, "info": r, "start": now, "seen": True, "last_run": now,
                                      "why": r.get("why", "kenar çubuğu")}
                    log("Çalışıyor sayıldı: %s (neden: %s)" % (k, r.get("why", "kenar çubuğu")))
            for k in list(self.active):
                if k in running:
                    continue
                st = self.active[k]
                if k in present:
                    if not st.get("seen") and now < st.get("deadline", 0):
                        continue  # gönderilen mesaj henüz çalışmaya başlamadı, bekle
                    st["miss"] += 1
                    quiet = now - st.get("last_run", st.get("start", now))
                    if st["miss"] >= self.confirm or (st["miss"] >= 2 and quiet >= 25 and st.get("seen")):
                        done.append((k, self.active.pop(k)))
                else:
                    st["gone"] += 1
                    if st["gone"] > 400:
                        del self.active[k]
        for k, st in done:
            self.finish(k, st)

    # --- onay bekleme / takılma / uzun sürme
    def inspect(self, pc, running):
        now = time.time()
        key = pc[0] + "||" + pc[1]
        text = self.read_quick()
        h = hashlib.md5(text[-1500:].encode("utf-8")).hexdigest()
        w = self.watch.setdefault(key, {"hash": None, "since": now, "stuck": False, "long": False, "ap": None, "ap_t": 0})
        if h != w["hash"]:
            w.update(hash=h, since=now, stuck=False)
        is_run = key in running
        cid = hid(*pc)
        self.convs[cid] = pc
        btns = self.with_cdp(lambda: self.cdp.agk("approvals()")) or []
        texts = sorted({b["text"] for b in btns})
        tail = text[-700:]
        lm = LIMIT_RE.search(tail)
        if lm:
            lsig = hashlib.md5(tail[max(0, lm.start() - 60):lm.end() + 60].encode("utf-8")).hexdigest()
            if w.get("lim") != lsig:
                w["lim"] = lsig
                mins = limit_wait_minutes(tail)
                self.say("🚧 Limit / kota / yoğunluk uyarısı\n\n📁 Proje: %s\n💬 Sohbet: %s\n\n%s" % (
                    pc[0], pc[1], short(" ".join(tail.strip().splitlines()[-3:]), 300)),
                    [[{"text": "🔁 Tekrar dene", "callback_data": "rt:" + cid},
                      {"text": "⏰ %s sonra devam et" % fmt_dur(mins * 60), "callback_data": "lz:%s:%d" % (cid, mins)}]])
        if texts:
            sig = tuple(texts)
            if sig != w["ap"] or now - w["ap_t"] > 1800:
                w["ap"], w["ap_t"] = sig, now
                if self.auto_approve(pc, cid, text, texts):
                    return
                self.ap_cache[cid] = texts
                ctx = short(" ".join(text.strip().splitlines()[-4:]), 300)
                kb = []
                for i, t in enumerate(texts):
                    good = re.match(r"^(approve|allow|accept|run|onayla|izin|kabul|çalıştır)", t, re.I)
                    kb.append([{"text": ("✅ " if good else "❌ ") + t, "callback_data": "ap:%s:%d" % (cid, i)}])
                kb.append([{"text": "📥 Cevabı gör", "callback_data": "a:" + cid}])
                self.say("⏸ Ajan senden onay bekliyor\n\n📁 Proje: %s\n💬 Sohbet: %s\n\n%s\n\nDüğmeye basarsan ben tıklarım." % (
                    pc[0], pc[1], ctx), kb)
        else:
            w["ap"] = None
        if is_run and not w["stuck"] and now - w["since"] >= self.stuck_min * 60:
            w["stuck"] = True
            kb = [[{"text": "📜 Son işlemler", "callback_data": "l:" + cid},
                   {"text": "🛑 Durdur", "callback_data": "s:" + cid}]]
            self.say("⚠️ Takılmış olabilir\n\n📁 Proje: %s\n💬 Sohbet: %s\n\n%s dakikadır sohbette yeni bir şey görünmüyor. "
                     "(Uzun süren tek bir işlem de olabilir.)" % (pc[0], pc[1], fmt_dur(now - w["since"])), kb)

    def auto_approve(self, pc, cid, text, texts):
        """İzin verdiğin güvenli komutlar için (ayarlar.env: OTO_ONAY) onayı kendisi verir. Varsayılan kapalı."""
        rules = [x.strip().lower() for x in re.split(r"[;|]", self.cfg.get("OTO_ONAY", "")) if x.strip()]
        if not rules:
            return False
        ctx = " ".join(text.strip().splitlines()[-6:]).lower()
        hit = next((r for r in rules if r in ctx), None)
        if not hit or DANGER_RE.search(ctx):
            return False
        good = [t for t in texts if re.match(r"^(approve|allow|accept|onayla|izin ver|kabul)", t, re.I) and "always" not in t.lower()]
        if not good:
            return False
        pos = self.with_cdp(lambda: self.cdp.agk("approvalPos(%s)" % json.dumps(good[0])))
        if not pos:
            return False
        self.cdp.click(pos["x"], pos["y"])
        self.say("🤖 Otomatik onaylandı (kural: %s)\n📁 %s\n💬 %s\n«%s»" % (hit, pc[0], pc[1], short(ctx, 200)))
        log("Otomatik onay: %s / %s (%s)" % (pc[0], pc[1], hit))
        return True

    def watch_cycle(self):
        now = time.time()
        running = {conv_key(r): r for r in self.rows if r["kind"] == "conv" and r.get("running")}
        for k in list(self.watch):
            if k not in running and k not in self.active:
                self.watch.pop(k, None)
        # uzun süren işler
        for k, st in list(self.active.items()):
            w = self.watch.setdefault(k, {"hash": None, "since": now, "stuck": False, "long": False, "ap": None, "ap_t": 0})
            if k in running and not w["long"] and now - st.get("start", now) >= self.long_min * 60:
                w["long"] = True
                pr, ti = k.split("||", 1)
                cid = hid(pr, ti)
                self.convs[cid] = (pr, ti)
                self.say("⏳ Uzun süredir çalışıyor\n\n📁 Proje: %s\n💬 Sohbet: %s\n⏱ %s" % (pr, ti, fmt_dur(now - st["start"])),
                         [[{"text": "📜 Son işlemler", "callback_data": "l:" + cid}]])
        open_pc = None
        with self.lock:
            open_pc = self.current_open()
            if open_pc:
                self.inspect(open_pc, running)
            # sen bilgisayarda değilsen çalışan diğer sohbetlere de bak
            if running and self.user_away() and now - self.last_visit >= self.visit_every:
                keys = [k for k in running if tuple(k.split("||", 1)) != open_pc]
                if keys:
                    self.last_visit = now
                    k = keys[self.visit_i % len(keys)]
                    self.visit_i += 1
                    pr, ti = k.split("||", 1)
                    self.open_conv(pr, ti)
                    self.inspect((pr, ti), running)

    def dump_diag(self):
        """Arayüz teşhisini teshis_oto.json'a yazar (sorun gidermek için)."""
        try:
            with self.lock:
                d = self.with_cdp(lambda: self.cdp.agk("diag()")) or {}
            d["bridge_rows"] = self.rows
            d["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            d["version"] = "6.1"
            (BASE / "teshis_oto.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log("Teşhis yazılamadı: %s" % e)

    def remember_exe(self):
        try:
            if self.cfg.get("ANTIGRAVITY_EXE"):
                return
            exe = detect_port_exe(self.cfg["CDP_PORT"])
            if exe:
                save_env_value("ANTIGRAVITY_EXE", exe)
                self.cfg["ANTIGRAVITY_EXE"] = exe
                log("Antigravity yolu kaydedildi: %s" % exe)
        except Exception as e:
            log("Antigravity yolu alınamadı: %s" % e)

    def watchdog(self):
        """Antigravity kapanmışsa (sen uzaktayken) köprü portuyla yeniden açar."""
        if self.cfg.get("AUTO_RESTART", "1") == "0" or not IS_WIN or self.down_since is None:
            return
        now = time.time()
        if now - self.down_since < float(self.cfg.get("WATCHDOG_SEC", "90")):
            return
        if idle_seconds() < 300:
            return
        self.restarts = [t for t in self.restarts if now - t < 3600]
        if len(self.restarts) >= 3:
            return
        if antigravity_running():
            if not self.warned_noport:
                self.warned_noport = True
                self.say("⚠️ Antigravity açık ama köprü portu yok (normal simgeyle açılmış gibi).\n"
                         "Köprülü yeniden açmak için /yenidenbaslat yaz. DİKKAT: çalışan işler yarıda kalır.")
            return
        exe = pick_exe(find_antigravity_exes(self.cfg), self.cfg)
        if not exe:
            return
        launch_antigravity(exe, self.cfg)
        self.restarts.append(now)
        self.down_since = now
        self.say("🔄 Antigravity kapanmıştı, köprü portuyla yeniden açtım.")

    def monitor_loop(self):
        started = False
        while True:
            try:
                self.last_scan = time.time()
                self.scan_once()
                if self.digest and not self.quiet_now():
                    self.flush_digest()
                try:
                    self.watch_cycle()
                except CDPError:
                    raise
                except Exception:
                    log("İzleme hatası:\n" + traceback.format_exc())
                self.down_since = None
                self.warned_noport = False
                if time.time() - self.last_diag > 900:
                    self.last_diag = time.time()
                    self.dump_diag()
                if self.cdp_ok is not True:
                    self.cdp_ok = True
                    n = len(self.active)
                    log("Antigravity'ye bağlandı, çalışan iş: %d" % n)
                    if IS_WIN:
                        threading.Thread(target=self.remember_exe, daemon=True).start()
                    if not started:
                        self.say("🟢 Köprü hazır. Antigravity'ye bağlandım.\nŞu an çalışan iş: %d\n\n📁 Projeler düğmesiyle başla." % n,
                                 KEYBOARD)
                    else:
                        self.say("🟢 Antigravity'ye yeniden bağlandım.")
                    started = True
            except (CDPError, UIError) as e:
                if self.down_since is None:
                    self.down_since = time.time()
                if self.cdp_ok is not False:
                    self.cdp_ok = False
                    log("CDP yok: %s" % e)
                    self.say("🔴 Antigravity'ye bağlanamıyorum. Antigravity'yi masaüstündeki "
                             "\"Antigravity (Telegram)\" kısayolundan açtığından emin ol.\n(%s)" % e)
                try:
                    self.watchdog()
                except Exception:
                    log("Bekçi hatası:\n" + traceback.format_exc())
                time.sleep(8)
            except Exception:
                log("Beklenmeyen hata:\n" + traceback.format_exc())
                time.sleep(5)
            self.idle_wait(self.poll)

    def idle_wait(self, secs):
        """Bekler; sayfadaki DOM değişimi uyarısı gelirse (en az 1 sn arayla) erken uyanır."""
        end = time.time() + secs
        while time.time() < end:
            if self.cdp.ping > self.last_scan and time.time() - self.last_scan >= 1.0:
                return
            if self.cdp.connected():
                self.cdp.pump(0.25)
            else:
                time.sleep(0.25)

    def quiet_now(self):
        m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", self.cfg.get("SESSIZ", "").strip())
        if not m:
            return False
        a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
        lt = time.localtime()
        n = lt.tm_hour * 60 + lt.tm_min
        return (a <= n < b) if a < b else (n >= a or n < b)

    def flush_digest(self):
        items, self.digest = self.digest, []
        if not items:
            return
        kb = []
        for pr, ti, ts in items[:12]:
            cid = hid(pr, ti)
            self.convs[cid] = (pr, ti)
            kb.append([{"text": "📥 %s · %s · %s" % (time.strftime("%H:%M", time.localtime(ts)), short(pr, 12), short(ti, 26)),
                        "callback_data": "a:" + cid}])
        self.say("🌅 Sessiz saatte biten işler (%d)\nCevabını çekmek için dokun:" % len(items), kb)

    # ------------------------------------------------------------------ Telegram: cevaplar
    def send_answer(self, chat, cid, tail=False):
        pc = self.convs.get(cid)
        if not pc:
            self.tg.send_text(chat, "Bu sohbeti bulamadım. 📁 Projeler'den tekrar seç.")
            return
        with self.lock:
            self.open_conv(*pc)
            body = self.read_tail() if tail else self.read_answer()
        self.select(pc)
        head = ("📜 Son işlemler" if tail else "📥 Son cevap") + "\n📁 %s\n💬 %s\n\n" % pc
        kb = [[{"text": "✍️ Yaz", "callback_data": "c:" + cid},
               {"text": "📁 Projeler", "callback_data": "home"}]]
        if not tail:
            kb = self.file_buttons(self.existing_files(body)) + kb
        if len(body) > int(self.cfg.get("UZUN_CEVAP", "6000")):
            self.tg.send_document(chat, "cevap.md", (head + body).encode("utf-8"), "Tam cevap (uzun olduğu için dosya)")
            self.tg.send_text(chat, head + body[:1500] + "\n\n… (devamı yukarıdaki cevap.md dosyasında)", kb)
        else:
            self.tg.send_text(chat, head + body, kb)

    def send_files_of(self, chat, cid):
        pc = self.convs.get(cid)
        if not pc:
            self.tg.send_text(chat, "Bu sohbeti bulamadım.")
            return
        with self.lock:
            self.open_conv(*pc)
            raw = self.read_raw()
        files = self.existing_files(raw, limit=8)
        if not files:
            self.tg.send_text(chat, "Bu sohbette bilgisayarda bulunan bir dosya yolu göremedim.\n"
                                    "Elle almak için: /dosya <yol veya klasör>")
            return
        self.tg.send_text(chat, "📂 Sohbette geçen dosyalar:\n💬 %s" % pc[1], self.file_buttons(files))

    def do_stop(self, cid):
        pc = self.convs.get(cid)
        if not pc:
            return "Bu sohbeti bulamadım."
        with self.lock:
            self.open_conv(*pc)

            def f():
                pos = self.cdp.agk("stopCenter()")
                if not pos:
                    return False
                self.cdp.click(pos["x"], pos["y"])
                return True
            ok = self.with_cdp(f)
        return ("🛑 Durdurma düğmesine basıldı: %s" % pc[1]) if ok else "Durdurulacak çalışan iş bulamadım."

    def do_approve(self, chat, cid, idx):
        pc = self.convs.get(cid)
        texts = self.ap_cache.get(cid) or []
        if not pc or idx >= len(texts):
            self.tg.send_text(chat, "Bu onay isteği artık geçerli değil.")
            return
        t = texts[idx]
        with self.lock:
            self.open_conv(*pc)

            def f():
                pos = self.cdp.agk("approvalPos(%s)" % json.dumps(t))
                if not pos:
                    return False
                self.cdp.click(pos["x"], pos["y"])
                return True
            ok = self.with_cdp(f)
        if ok:
            w = self.watch.get(pc[0] + "||" + pc[1])
            if w:
                w["ap"] = None
        self.tg.send_text(chat, ("✅ Tıkladım: %s\n💬 %s" % (t, pc[1])) if ok else
                          "Düğmeyi bulamadım (başka biri onaylamış olabilir).")

    # ------------------------------------------------------------------ yazma / güvenlik
    def confirm_then(self, text, action):
        self.danger = (action, time.time())
        kb = [[{"text": "✅ Evet, gönder", "callback_data": "dok"}, {"text": "✖️ Vazgeç", "callback_data": "dno"}]]
        self.say("⚠️ Bu mesaj tehlikeli olabilecek bir ifade içeriyor:\n\n«%s»\n\nYine de çalıştırılsın mı?" % short(text, 400), kb)

    def guard(self, text, go):
        if self.safe_mode and DANGER_RE.search(text or ""):
            self.confirm_then(text, go)
        else:
            go()

    def deliver(self, sel, text):
        def go():
            paths = [f["path"] for f in self.files]
            res = self.write_to(sel[0], sel[1], text, paths)
            self.pending = None
            self.files = []
            self.say("✍️ Gönderildi\n📁 %s\n💬 %s%s\n\nBitince \"✅ Bot işlemi bitirdi\" bildirimi gelir." % (
                sel[0], sel[1], self.attach_note(len(paths), res)),
                [[{"text": "🔁 Başka sohbet seç", "callback_data": "home"}]])
        self.guard(text, go)

    def deliver_new(self, project, text):
        def go():
            paths = [f["path"] for f in self.files]
            res = self.start_new(project, text, paths)
            self.pending = None
            self.files = []
            self.say("➕ Yeni sohbet açıldı ve mesaj gönderildi\n📁 %s%s\n\nBitince \"✅ Bot işlemi bitirdi\" bildirimi gelir." % (
                project, self.attach_note(len(paths), res)),
                [[{"text": "📁 Projeler", "callback_data": "home"}]])
        if project == WIZ_MARK:
            inner = go

            def go():
                def work():
                    try:
                        inner()
                    except (UIError, CDPError) as e:
                        self.say("⚠️ Antigravity ile konuşamadım: %s" % e)
                    except Exception as e:
                        log("Sihirbaz hatası: %s" % traceback.format_exc())
                        self.say("⚠️ Beklenmeyen hata: %s" % e)
                threading.Thread(target=work, daemon=True).start()
        self.guard(text, go)

    @staticmethod
    def attach_note(n, res):
        if not n:
            return ""
        if res["bad"] == 0:
            return "\n📎 %d dosya Antigravity'ye ek olarak eklendi" % res["ok"]
        if res["ok"] == 0:
            return "\n📎 %d dosya yol olarak yazıldı (ek olarak eklenemedi)" % res["bad"]
        return "\n📎 %d dosya ek olarak eklendi, %d tanesi yol olarak yazıldı" % (res["ok"], res["bad"])

    def do_write(self, text):
        self.voice_pending = None
        np = self.newproj_valid()
        if np:
            self.newproj = None
            self.deliver_new(np, text)
            return
        sel = self.selected_valid()
        if not sel:
            self.pending = text or self.pending
            note = "📝 Mesajın kaydedildi:\n«%s»\n\n" % short(text, 200) if text else ""
            if self.files:
                note += "📎 %d dosya kaydedildi.\n" % len(self.files)
            t, kb = self.projects_menu(note + "Hangi sohbete gitsin? Proje, sonra sohbet seç.")
            self.say(t, kb)
            return
        self.deliver(sel, text)

    # ------------------------------------------------------------------ gelen dosyalar (Telegram'dan bilgisayara)
    def save_incoming(self, chat, media, queue=True):
        day = self.gelen / time.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        saved = []
        for it in media:
            if it["size"] and it["size"] > MAX_DL:
                self.tg.send_text(chat, "⚠️ %s çok büyük (%s). Telegram botları en fazla 20 MB indirebilir.\n"
                                        "Dosyayı bilgisayarda bir klasöre koyup bana yolunu yazabilirsin." % (it["name"], human_size(it["size"])))
                continue
            try:
                info = self.tg.get_file(it["file_id"])
                dest = day / ("%s_%s_%s" % (time.strftime("%H%M%S"), uuid.uuid4().hex[:4], safe_name(it["name"])))
                self.tg.download(info["file_path"], dest)
            except (TgError, OSError, urllib.error.URLError) as e:
                self.tg.send_text(chat, "⚠️ %s indirilemedi: %s" % (it["name"], e))
                continue
            entry = {"path": str(dest.resolve()), "kind": it["kind"], "name": it["name"], "size": dest.stat().st_size}
            if queue:
                with self.slock:
                    self.files.append(entry)
            log("Dosya alındı: %s (%s)" % (dest.name, human_size(dest.stat().st_size)))
            saved.append(entry)
        return saved

    def schedule_flush(self, chat):
        with self.slock:
            if self.flush_timer:
                self.flush_timer.cancel()
            self.flush_timer = threading.Timer(2.0, self.flush_files, args=(chat,))
            self.flush_timer.daemon = True
            self.flush_timer.start()

    def flush_files(self, chat):
        try:
            with self.slock:
                cap, self.cap = self.cap, None
                n = len(self.files)
            if not n:
                return
            if cap:
                self.do_write(cap)
                return
            np = self.newproj_valid()
            sel = self.selected_valid()
            if np:
                kb = [[{"text": "✖️ İptal", "callback_data": "cancel"}]]
                self.say("📎 %d dosya kaydedildi → yeni sohbet (%s)\nŞimdi ilk mesajını yaz, dosyalarla birlikte göndereyim." % (n, np), kb)
            elif sel:
                kb = [[{"text": "📤 Sadece dosyaları gönder", "callback_data": "fsend"},
                       {"text": "✖️ İptal", "callback_data": "cancel"}]]
                self.say("📎 %d dosya kaydedildi → %s / %s\nŞimdi talimatını yaz, dosyalarla birlikte göndereyim." % (
                    n, sel[0], sel[1]), kb)
            else:
                t, kb = self.projects_menu("📎 %d dosya kaydedildi.\nHangi sohbete gitsin? Proje, sonra sohbet seç.\n"
                                           "(İstersen önce talimatını da yazabilirsin.)" % n)
                self.say(t, kb)
        except (CDPError, UIError) as e:
            self.say("⚠️ Antigravity ile konuşamadım: %s" % e)
        except Exception as e:
            log("Dosya gönderme hatası:\n" + traceback.format_exc())
            self.say("⚠️ Hata: %s" % e)

    # --- sesli mesaj -> yazı
    def stt_ready(self):
        if self.cfg.get("STT_MODE", "auto") == "off":
            return False
        if self.cfg.get("OPENAI_API_KEY", "").strip():
            return True
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None

    def transcribe(self, path):
        if os.environ.get("AGK_FAKE_STT"):
            return os.environ["AGK_FAKE_STT"]
        key = self.cfg.get("OPENAI_API_KEY", "").strip()
        lang = self.cfg.get("STT_DIL", "tr")
        if key:
            boundary = "----agk" + uuid.uuid4().hex
            body = b""
            for k, v in (("model", "whisper-1"), ("language", lang)):
                body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode("utf-8")
            body += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
                     % (boundary, Path(path).name)).encode("utf-8")
            body += Path(path).read_bytes() + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
            req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", body, {
                "Authorization": "Bearer " + key, "Content-Type": "multipart/form-data; boundary=" + boundary})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8")).get("text", "")
        if self._stt_model is None:
            from faster_whisper import WhisperModel
            self._stt_model = WhisperModel(self.cfg.get("STT_MODEL", "small"), device="cpu", compute_type="int8")
        segs, _ = self._stt_model.transcribe(str(path), language=lang, vad_filter=True)
        return " ".join(s.text.strip() for s in segs)

    def handle_voice(self, chat, msg):
        saved = self.save_incoming(chat, extract_media({"voice": msg["voice"]}), queue=False)
        if not saved:
            return
        self.tg.send_text(chat, "🎙 Yazıya çeviriyorum…")
        try:
            text = (self.transcribe(saved[0]["path"]) or "").strip()
        except Exception as e:
            log("Ses çevrilemedi: %s" % e)
            with self.slock:
                self.files.append(saved[0])
            self.tg.send_text(chat, "⚠️ Sesi yazıya çeviremedim (%s).\nDosya olarak kaydettim; bir açıklama yazarsan dosyayla birlikte gönderirim." % short(str(e), 120))
            return
        if not text:
            self.tg.send_text(chat, "Sesten bir şey anlayamadım. Tekrar dener misin?")
            return
        self.voice_pending = text
        kb = [[{"text": "✅ Gönder", "callback_data": "vg"}, {"text": "✖️ Vazgeç", "callback_data": "vx"}]]
        self.tg.send_text(chat, "🎙 Yazıya çevrildi:\n«%s»\n\nDoğruysa Gönder'e bas. Yanlışsa düzeltip normal mesaj olarak yaz." % text, kb)

    # ------------------------------------------------------------------ zamanlama
    def sched_load(self):
        try:
            self.sched = json.loads(self.sched_file.read_text(encoding="utf-8"))
        except Exception:
            self.sched = []

    def sched_save(self):
        try:
            self.sched_file.write_text(json.dumps(self.sched, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log("Zamanlama kaydedilemedi: %s" % e)

    def cmd_zamanla(self, chat, arg):
        usage = ("Kullanım:\n/zamanla 03:00 mesajın  → bir sonraki 03:00'te gönderir\n"
                 "/zamanla yarın 09:30 mesajın\n/zamanla +45 mesajın  → 45 dakika sonra\n"
                 "(Önce bir sohbet seçmelisin. Mesaj o sohbete gider.)")
        if not (arg or "").strip():
            self.tg.send_text(chat, usage)
            return
        w = parse_when(arg)
        if not w:
            self.tg.send_text(chat, "Zamanı anlayamadım.\n\n" + usage)
            return
        sel = self.selected_valid()
        if not sel:
            self.tg.send_text(chat, *self.projects_menu("Önce mesajın gideceği sohbeti seç, sonra /zamanla komutunu tekrar yaz."))
            return
        ts, text = w

        def go():
            with self.slock:
                self.sched.append({"id": uuid.uuid4().hex[:6], "ts": ts, "project": sel[0], "title": sel[1], "text": text, "tries": 0})
                self.sched.sort(key=lambda s: s["ts"])
                self.sched_save()
            self.say("⏰ Zamanlandı\n🕐 %s\n📁 %s\n💬 %s\n«%s»" % (time.strftime("%d.%m %H:%M", time.localtime(ts)),
                                                                sel[0], sel[1], short(text, 300)))
        self.guard(text, go)

    def sched_menu(self):
        if not self.sched:
            return "Zamanlanmış mesaj yok. ⏰\nEklemek için: /zamanla 03:00 mesajın", None
        kb, lines = [], ["⏰ Zamanlanmış mesajlar:"]
        for s in self.sched:
            hh = time.strftime("%d.%m %H:%M", time.localtime(s["ts"]))
            kb.append([{"text": "🗑 %s · %s · %s" % (hh, short(s["title"], 18), short(s["text"], 22)), "callback_data": "zd:" + s["id"]}])
        return "\n".join(lines) + "\nSilmek istediğine dokun:", kb

    def run_due(self):
        now = time.time()
        with self.slock:
            due = [s for s in self.sched if s["ts"] <= now]
        for s in due:
            try:
                self.write_to(s["project"], s["title"], s["text"])
                with self.slock:
                    self.sched = [x for x in self.sched if x["id"] != s["id"]]
                    self.sched_save()
                self.say("⏰ Zamanlanmış mesaj gönderildi\n📁 %s\n💬 %s\n«%s»" % (s["project"], s["title"], short(s["text"], 200)))
            except (CDPError, UIError) as e:
                s["tries"] = s.get("tries", 0) + 1
                with self.slock:
                    if s["tries"] >= 20:
                        self.sched = [x for x in self.sched if x["id"] != s["id"]]
                        self.say("⚠️ Zamanlanmış mesaj gönderilemedi (10 dk denedim): %s\n«%s»" % (e, short(s["text"], 200)))
                    else:
                        s["ts"] = now + 30
                    self.sched_save()

    def scheduler_loop(self):
        tick = float(self.cfg.get("SCHED_TICK", "15"))
        while True:
            time.sleep(tick)
            try:
                self.run_due()
            except Exception:
                log("Zamanlayıcı hatası:\n" + traceback.format_exc())

    # ------------------------------------------------------------------ bakım
    def cleanup_gelen(self):
        days = int(float(self.cfg.get("GELEN_GUN", "7")))
        if days <= 0 or not self.gelen.exists():
            return 0
        n = 0
        limit = time.time() - days * 86400
        for d in self.gelen.iterdir():
            if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name):
                try:
                    if d.stat().st_mtime < limit:
                        shutil.rmtree(d, ignore_errors=True)
                        n += 1
                except OSError:
                    pass
        if n:
            log("Eski 'gelen' klasörleri silindi: %d" % n)
        return n

    def housekeeping_loop(self):
        while True:
            try:
                self.cleanup_gelen()
            except Exception:
                log("Temizlik hatası:\n" + traceback.format_exc())
            time.sleep(6 * 3600)

    def alert_unauth(self, frm, name):
        if not self.user_id:
            return
        now = time.time()
        if now - self.unauth_seen.get(frm, 0) < 3600:
            return
        self.unauth_seen[frm] = now
        self.say("🚨 Yetkisiz bir kişi botuna erişmeye çalıştı\nKullanıcı no: %s\nAd: %s\nHiçbir komut çalıştırılmadı." % (frm, name or "?"))

    # ------------------------------------------------------------------ Telegram: komutlar
    # ------------------------------------------------------------------ proje klasörü, git, hata, test
    def proj_dir(self):
        """Seçili projenin bilgisayardaki klasörünü bulmaya çalışır (bulamazsa None)."""
        sel = self.selected_valid()
        name = sel[0] if sel else None
        if not name or name in (NEW_GENERAL, "No Project"):
            return None, name
        home = Path.home()
        bases = [home / "Desktop", home / "Desktop" / "Uygulamalarım", home / "Desktop" / "Projelerim",
                 home / "Documents", home / "Downloads", home]
        for x in re.split(r"[;|]", self.cfg.get("PROJE_KOKLERI", "")):
            if x.strip():
                bases.insert(0, Path(x.strip()))
        saved = self.proj_map_load().get(name)
        if saved and Path(saved).is_dir():
            return Path(saved), name
        for b in bases:
            try:
                c = b / name
                if c.is_dir():
                    return c, name
            except Exception:
                pass
        # Masaüstü altında 3 seviye derinliğe kadar ara (örn. uygulamalarım\mobil\izlepro)
        want = name.lower()
        for b in (home / "Desktop", home / "Documents"):
            try:
                for root, dirs, _ in os.walk(str(b)):
                    depth = len(Path(root).relative_to(b).parts)
                    dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")] if depth < 3 else []
                    for d in dirs:
                        if d.lower() == want:
                            return Path(root) / d, name
            except Exception:
                pass
        return None, name

    def cmd_git(self, chat):
        d, name = self.proj_dir()
        if not name:
            self.tg.send_text(chat, *self.projects_menu("Önce bir proje/sohbet seç."))
            return
        if not d:
            self.tg.send_text(chat, "'%s' klasörünü bulamadım. 📂 /klasor ile klasörünü bul, \"📌 Seçili projenin klasörü yap\" düğmesine bas; bir kere yeter." % name,
                              [[{"text": "📂 Klasörlere göz at", "callback_data": "kb:0"}]])
            return
        r = git_summary(d)
        self.tg.send_text(chat, ("📁 %s\n" % name + r) if r else "📁 %s\nBu klasör git deposu değil (ya da git kurulu değil)." % name)

    def cmd_hata(self, chat):
        txt = self.read_quick() or ""
        errs = []
        for line in txt.splitlines():
            if re.search(r"\b(Error|Exception|Traceback|AssertionError|Failed|errno|SyntaxError|hata|başarısız)\b", line, re.I):
                errs.append(line.strip()[:300])
        errs = list(dict.fromkeys(errs))[-15:]
        if errs:
            self.tg.send_text(chat, "⚠️ Seçili sohbette görülen son hatalar:\n\n" + "\n".join("• " + e for e in errs))
        else:
            self.tg.send_text(chat, "✅ Açık sohbette hata satırı görmedim.\n(Yalnızca ekranda görünen yazıyı tarar; tam kontrol için /son.)")

    def test_cmd_for(self, d):
        if (d / "pytest.ini").exists() or list(d.glob("test_*.py")) or (d / "tests").is_dir():
            return [sys.executable or "python", "-m", "pytest", "-q"]
        if (d / "package.json").exists():
            return ["npm.cmd" if IS_WIN else "npm", "test"]
        if (d / "Cargo.toml").exists():
            return ["cargo", "test"]
        if (d / "go.mod").exists():
            return ["go", "test", "./..."]
        if (d / "pubspec.yaml").exists():
            return ["flutter", "test"]
        return None

    def cmd_test(self, chat):
        d, name = self.proj_dir()
        if not d:
            self.tg.send_text(chat, "Önce klasörü bulunabilen bir proje seç (/menu). Gerekirse ayarlar.env → PROJE_KOKLERI.")
            return
        c = self.test_cmd_for(d)
        if not c:
            self.tg.send_text(chat, "📁 %s\nTest dosyası/yapılandırması bulamadım (pytest, npm, cargo, go, flutter)." % name)
            return
        kb = [[{"text": "▶️ Testleri çalıştır", "callback_data": "ts:go"}, {"text": "✖️ Vazgeç", "callback_data": "cancel"}]]
        self.tg.send_text(chat, "🧪 %s\nÇalışacak komut: %s\nKlasör: %s\n\nBu komut bilgisayarında kod çalıştırır. Onaylıyor musun?" % (name, " ".join(c), d), kb)

    def do_test(self, chat):
        d, name = self.proj_dir()
        c = self.test_cmd_for(d) if d else None
        if not c:
            self.tg.send_text(chat, "Proje/komut artık bulunamadı. /test ile yeniden dene.")
            return
        self.tg.send_text(chat, "🧪 '%s' testleri çalışıyor… (en çok 3 dk)" % name)
        try:
            r = subprocess.run(c, cwd=str(d), capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=180, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            raw = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            head = "✅ TESTLER GEÇTİ" if r.returncode == 0 else "❌ TESTLER BAŞARISIZ (kod %s)" % r.returncode
            self.tg.send_text(chat, "🧪 %s\n%s\n\n%s" % (name, head, raw[-2500:]))
        except subprocess.TimeoutExpired:
            self.tg.send_text(chat, "⚠️ Testler 3 dakikada bitmedi, durdurdum.")
        except Exception as e:
            self.tg.send_text(chat, "⚠️ Test çalıştırılamadı: %s" % e)

    # ------------------------------------------------------------------ PIN kilidi
    @staticmethod
    def pin_hash(pin):
        return hashlib.sha256(("agk-pin:" + pin).encode("utf-8")).hexdigest()

    def pin_on(self):
        return bool(self.cfg.get("PIN_HASH"))

    def pin_block(self, chat, label):
        """PIN açıksa ve kilit açılmamışsa True döner (işlem yapılmaz, PIN istenir)."""
        if not self.pin_on() or time.time() < self.pin_until:
            return False
        now = time.time()
        self.pin_fail = [t for t in self.pin_fail if now - t < 600]
        if len(self.pin_fail) >= 3:
            self.tg.send_text(chat, "⛔ 3 yanlış PIN. 10 dakika boyunca korumalı işlemler kilitli.")
            return True
        self.pin_wait = (label, now)
        self.tg.send_text(chat, "🔐 Bu işlem PIN ister.\nPIN'ini yaz (mesajı silerim). Sonra işlemi tekrar başlat.\nİşlem: %s" % label)
        return True

    def pin_try(self, chat, text):
        """Bekleyen PIN varsa mesajı PIN olarak dener; ele alındıysa True."""
        if not self.pin_wait or time.time() - self.pin_wait[1] > 120:
            self.pin_wait = None
            return False
        t = text.strip()
        if not re.match(r"^\d{4,8}$", t):
            return False
        self.delete_cur()
        now = time.time()
        self.pin_fail = [x for x in self.pin_fail if now - x < 600]
        if len(self.pin_fail) >= 3:
            self.tg.send_text(chat, "⛔ 3 yanlış PIN. 10 dakika kilitli.")
            return True
        if self.pin_hash(t) == self.cfg.get("PIN_HASH"):
            self.pin_until = now + 600
            self.pin_wait = None
            self.pin_fail = []
            self.tg.send_text(chat, "🔓 PIN doğru. 10 dakika boyunca korumalı işlemler açık. Şimdi işlemi tekrar başlat.")
        else:
            self.pin_fail.append(now)
            left = 3 - len(self.pin_fail)
            self.tg.send_text(chat, ("❌ Yanlış PIN. Kalan hak: %d" % left) if left > 0 else "⛔ 3 yanlış PIN. 10 dakika kilitli.")
        return True

    def delete_cur(self):
        try:
            if self.cur_msg and self.cur_msg[1]:
                self.tg.call("deleteMessage", {"chat_id": self.cur_msg[0], "message_id": self.cur_msg[1]})
        except Exception:
            pass

    def cmd_pin(self, chat, arg):
        a = (arg or "").strip()
        if re.match(r"^\d{4,8}$", a):
            self.delete_cur()
            if self.pin_on() and time.time() >= self.pin_until:
                self.pin_block(chat, "PIN değiştir")
                self.tg.send_text(chat, "PIN zaten ayarlı. Değiştirmek için önce eski PIN'i yaz, sonra /pin yeni_pin gönder.")
                return
            h = self.pin_hash(a)
            save_env_value("PIN_HASH", h)
            self.cfg["PIN_HASH"] = h
            self.pin_until = time.time() + 600
            self.tg.send_text(chat, "🔐 PIN ayarlandı (yazdığın mesajı sildim). Artık /yayinla, /surum, /test, /kilitle, /guncelle, "
                                    "/depoyukle, /yenidenbaslat için PIN sorulur; doğru girince 10 dk açık kalır.")
        elif a.lower() in ("kapat", "off", "sil"):
            if self.pin_block(chat, "PIN'i kapat"):
                return
            save_env_value("PIN_HASH", "")
            self.cfg["PIN_HASH"] = ""
            self.tg.send_text(chat, "🔓 PIN kapatıldı.")
        else:
            self.tg.send_text(chat, "🔐 PIN: %s\nKullanım: /pin 1234 (4-8 rakam)  ·  /pin kapat" % ("açık" if self.pin_on() else "kapalı"))

    # ------------------------------------------------------------------ GitHub deposuna dosya yükle
    def gh_cfg(self):
        try:
            return json.loads((self.gh_dir() / "ayarlar.json").read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def cmd_depoyukle(self, chat, arg):
        a = (arg or "").strip().strip('"')
        sel_ = self.selected_valid()
        repo = (self.dep_map_load().get(sel_[0], {}).get("depo") if sel_ else None) or self.gh_cfg().get("depo")
        if not repo:
            self.tg.send_text(chat, "Depo ayarı yok (githubbot\\ayarlar.json içinde \"depo\" olmalı).")
            return
        if not a:
            self.tg.send_text(chat, "Kullanım: /depoyukle C:\\yol\\dosya.apk\n%s deposunun EN SON sürümüne ek olarak yükler (aynı adlı dosyanın üstüne yazar)." % repo)
            return
        p = Path(a)
        if not p.is_file():
            self.tg.send_text(chat, "Dosyayı bulamadım: %s" % a)
            return
        if not path_allowed(p, self.roots):
            self.tg.send_text(chat, "⚠️ Bu dosya izinli klasörlerin dışında ya da gizli bir dosya.")
            return
        if self.pin_block(chat, "/depoyukle"):
            return
        mb = p.stat().st_size / 1048576.0
        if mb > 1900:
            self.tg.send_text(chat, "⚠️ Dosya %.0f MB; GitHub sınırı 2 GB." % mb)
            return
        self.dy_pending = (str(p), time.time())
        kb = [[{"text": "⬆️ Yükle", "callback_data": "dy:go"}, {"text": "✖️ Vazgeç", "callback_data": "cancel"}]]
        self.tg.send_text(chat, "📦 Depoya yükleme\n%s\nDosya: %s (%.1f MB)\nDepo: %s (en son sürüm)\n\nOnaylıyor musun?" % (LINE, p.name, mb, repo), kb)

    def do_depoyukle(self, chat):
        if not self.dy_pending or time.time() - self.dy_pending[1] > 600:
            self.tg.send_text(chat, "Süre doldu. /depoyukle ile yeniden başlat.")
            return
        path = self.dy_pending[0]
        self.dy_pending = None
        repo = self.gh_cfg().get("depo")
        if not repo or not Path(path).is_file():
            self.tg.send_text(chat, "Dosya ya da depo artık bulunamadı.")
            return
        self.tg.send_text(chat, "⬆️ Yükleniyor… büyük dosya birkaç dakika sürebilir.")

        def work():
            kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                r = subprocess.run(["gh", "release", "view", "--repo", repo, "--json", "tagName", "-q", ".tagName"], timeout=60, **kw)
                tag = (r.stdout or "").strip()
                if r.returncode != 0 or not tag:
                    self.tg.send_text(chat, "⚠️ Son sürüm bulunamadı (gh giriş yapılmış mı? gh auth login).\n" + short((r.stderr or "").strip(), 600))
                    return
                r = subprocess.run(["gh", "release", "upload", tag, path, "--repo", repo, "--clobber"], timeout=3600, **kw)
                if r.returncode == 0:
                    self.tg.send_text(chat, "🎉 Yüklendi\n%s\n%s → %s (%s)" % (LINE, Path(path).name, repo, tag))
                else:
                    self.tg.send_text(chat, "⚠️ Yüklenemedi:\n" + short((r.stderr or r.stdout or "").strip(), 900))
            except subprocess.TimeoutExpired:
                self.tg.send_text(chat, "⚠️ Zaman aşımı. GitHub'da sürümü kontrol et.")
            except Exception as e:
                self.tg.send_text(chat, "⚠️ Hata: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ günlük özet
    def ozet_text(self):
        today = time.strftime("%Y-%m-%d")
        done = [e for e in self.finished if time.strftime("%Y-%m-%d", time.localtime(e["ts"])) == today]
        lines = ["📋 Günlük özet · %s" % time.strftime("%d.%m.%Y"), LINE,
                 "✅ Bugün biten iş: %d" % len(done), "⏳ Şu an çalışan: %d" % len(self.active)]
        for e in done[:10]:
            lines.append("  • %s · %s / %s" % (time.strftime("%H:%M", time.localtime(e["ts"])), short(e["project"], 14), short(e["title"], 30)))
        if len(done) > 10:
            lines.append("  … ve %d iş daha" % (len(done) - 10))
        return "\n".join(lines)

    def cmd_ozet(self, chat, arg):
        a = (arg or "").strip().lower()
        if re.match(r"^\d{1,2}:\d{2}$", a):
            save_env_value("OZET", a)
            self.cfg["OZET"] = a
            self.tg.send_text(chat, "📋 Günlük özet her gün %s'de gelecek. Kapatmak: /ozet kapat" % a)
        elif a in ("kapat", "off"):
            save_env_value("OZET", "")
            self.cfg["OZET"] = ""
            self.tg.send_text(chat, "Günlük özet kapatıldı.")
        else:
            self.tg.send_text(chat, self.ozet_text() + "\n\nHer gün otomatik için: /ozet 21:00  (şu an: %s)" % (self.cfg.get("OZET") or "kapalı"))

    def ozet_loop(self):
        while True:
            try:
                t = (self.cfg.get("OZET") or "").strip()
                if t and self.user_id and re.match(r"^\d{1,2}:\d{2}$", t):
                    h, m = [int(x) for x in t.split(":")]
                    lt = time.localtime()
                    d = time.strftime("%Y-%m-%d")
                    if (lt.tm_hour, lt.tm_min) >= (h, m) and self.ozet_date != d:
                        self.ozet_date = d
                        self.say(self.ozet_text())
            except Exception:
                log("Özet hatası:\n" + traceback.format_exc())
            time.sleep(30)

    # ------------------------------------------------------------------ klasör gezgini
    def proj_map_load(self):
        try:
            return json.loads((BASE / "projeler.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def proj_map_save(self, d):
        try:
            (BASE / "projeler.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def browse_roots(self):
        home = Path.home()
        cands = [("🖥 Masaüstü", home / "Desktop"), ("📄 Belgeler", home / "Documents"), ("⬇️ İndirilenler", home / "Downloads")]
        for nm in ("Uygulamalarım", "Uygulamalarim", "Projelerim", "uygulamalar"):
            cands.append(("🗂 " + nm, home / "Desktop" / nm))
        for r in self.roots[:-1]:
            cands.append(("📁 " + Path(r).name, Path(r)))
        out, seen = [], set()
        for label, p in cands:
            try:
                if p.is_dir() and str(p.resolve()).lower() not in seen:
                    seen.add(str(p.resolve()).lower())
                    out.append((label, p))
            except Exception:
                pass
        return out

    def browse_view(self, path, page=0):
        """(metin, düğmeler) döner. path None ise başlangıç listesi."""
        if path is None:
            kb = [[{"text": l, "callback_data": "kb:" + self.reg_file(p)}] for l, p in self.browse_roots()]
            return "📂 Klasörler\n" + LINE + "\nBir yer seç; içinde gezinip dosya alabilir ya da projeni bağlayabilirsin.", kb
        p = Path(path)
        if not p.is_dir() or not path_allowed(p, self.roots):
            return "⚠️ Bu klasöre bakamam (yok, izinli yerlerin dışında ya da korumalı).", [[{"text": "📂 Başa dön", "callback_data": "kb:0"}]]
        try:
            subs = sorted([x for x in p.iterdir() if x.is_dir() and not x.name.startswith(".") and x.name.lower() not in SKIP_DIRS
                           and path_allowed(x, self.roots)], key=lambda x: x.name.lower())
            files = sorted([x for x in p.iterdir() if x.is_file() and not x.name.startswith(".")],
                           key=lambda x: x.stat().st_mtime, reverse=True)
        except Exception as e:
            return "⚠️ Klasör okunamadı: %s" % e, [[{"text": "📂 Başa dön", "callback_data": "kb:0"}]]
        per = 12
        pages = max(1, (len(subs) + per - 1) // per)
        page = max(0, min(page, pages - 1))
        kb, row = [], []
        for x in subs[page * per:(page + 1) * per]:
            row.append({"text": "📁 " + short(x.name, 18), "callback_data": "kb:" + self.reg_file(x)})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        if pages > 1:
            nav = []
            pid = self.reg_file(p)
            if page > 0:
                nav.append({"text": "◀️", "callback_data": "kp:%s:%d" % (pid, page - 1)})
            nav.append({"text": "%d/%d" % (page + 1, pages), "callback_data": "kp:%s:%d" % (pid, page)})
            if page < pages - 1:
                nav.append({"text": "▶️", "callback_data": "kp:%s:%d" % (pid, page + 1)})
            kb.append(nav)
        kb += self.file_buttons(files[:5])
        pid = self.reg_file(p)
        ctl = []
        if p.parent != p and path_allowed(p.parent, self.roots):
            ctl.append({"text": "⬆️ Üst", "callback_data": "kb:" + self.reg_file(p.parent)})
        ctl.append({"text": "🏠 Başa", "callback_data": "kb:0"})
        kb.append(ctl)
        act = [{"text": "🌿 Git durumu", "callback_data": "kg:" + pid}]
        if self.selected_valid() and self.selected_valid()[0] not in (NEW_GENERAL, "No Project"):
            act.append({"text": "📌 Seçili projenin klasörü yap", "callback_data": "kpin:" + pid})
        kb.append(act)
        kb.append([{"text": "🚀 Bu klasörü GitHub'a gönder (yeni depo)", "callback_data": "dpd:" + pid}])
        txt = "📂 %s\n%s\n%d klasör · %d dosya%s" % (p, LINE, len(subs), len(files),
                                              ("\n(en yeni %d dosya aşağıda)" % min(5, len(files))) if files else "")
        return txt, kb

    def cmd_klasor(self, chat, arg=""):
        a = (arg or "").strip().strip('"')
        if a:
            self.tg.send_text(chat, *self.browse_view(a))
        else:
            self.tg.send_text(chat, *self.browse_view(None))

    def pin_project_dir(self, chat, path):
        sel = self.selected_valid()
        p = Path(path)
        if not sel or not p.is_dir() or not path_allowed(p, self.roots):
            self.tg.send_text(chat, "Önce bir proje/sohbet seç (/menu), sonra klasörü bağla.")
            return
        m = self.proj_map_load()
        m[sel[0]] = str(p)
        self.proj_map_save(m)
        self.tg.send_text(chat, "📌 '%s' projesinin klasörü:\n%s\nArtık /git ve /test bu klasörde çalışır." % (sel[0], p))

    # ------------------------------------------------------------------ GitHub hesap ve yeni depo
    def run_cli(self, args, cwd=None, timeout=180):
        try:
            r = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.returncode, ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
        except FileNotFoundError:
            return 127, "%s bulunamadı (kurulu değil ya da PATH'te yok)." % args[0]
        except subprocess.TimeoutExpired:
            return 124, "Zaman aşımı."
        except Exception as e:
            return 1, str(e)

    def gh_accounts(self):
        """(aktif, [hesaplar]) döner."""
        code, out = self.run_cli(["gh", "auth", "status", "--hostname", "github.com"], timeout=30)
        accs, active, cur = [], None, None
        for line in out.splitlines():
            m = re.search(r"account\s+(\S+)", line) or re.search(r"Logged in to \S+ as\s+(\S+)", line)
            if m and "Logged in" in line:
                cur = m.group(1)
                accs.append(cur)
            if "Active account: true" in line and cur:
                active = cur
        if not active and len(accs) == 1:
            active = accs[0]
        return active, accs, code, out

    def cmd_hesap(self, chat):
        active, accs, code, out = self.gh_accounts()
        if not accs:
            self.tg.send_text(chat, "🐙 GitHub hesabı yok ya da gh kurulu değil.\nBilgisayarda bir kere: gh auth login (tarayıcıdan onaylanır). "
                                    "Bu adımı güvenlik için Telegram'dan yapmıyorum.")
            return
        kb = [[{"text": ("✅ " if a == active else "🔁 ") + a, "callback_data": "gha:" + a}] for a in accs[:8]]
        txt = "🐙 GitHub hesapları\n%s\nAktif: %s\n" % (LINE, active or "?")
        if len(accs) == 1:
            txt += "\nBaşka hesap eklemek için bilgisayarda bir kere `gh auth login` yap (ikinci hesapla). Sonra buradan düğmeyle geçersin."
        else:
            txt += "\nYayınlar ve yeni depolar AKTİF hesaba gider. Geçmek için hesaba dokun."
        self.tg.send_text(chat, txt, kb)

    def do_hesap(self, chat, name):
        active, accs, _, _ = self.gh_accounts()
        if name not in accs:
            self.tg.send_text(chat, "Bu hesap gh'de giriş yapılmış hesaplar arasında yok.")
            return
        code, out = self.run_cli(["gh", "auth", "switch", "--hostname", "github.com", "--user", name], timeout=30)
        self.tg.send_text(chat, ("✅ Aktif GitHub hesabı: %s" % name) if code == 0 else "⚠️ Hesap değiştirilemedi:\n" + short(out, 500))

    SENS_RE = re.compile(r"(^|/)(\.env(\..+)?|ayarlar\.env|key\.properties|id_rsa[^/]*|credentials\.json|service[-_]?account[^/]*\.json|[^/]*\.(jks|keystore|pfx|p12|pem|key))$", re.I)
    HEAVY_DIRS = {"build", "node_modules", ".dart_tool", ".gradle", "__pycache__", "venv", ".venv", ".idea", "intermediates", "whatsapp-profil", "gelen"}
    TEXT_EXT = {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".txt", ".md", ".dart", ".kt", ".java", ".properties", ".xml", ".cfg",
                ".ini", ".ps1", ".bat", ".html", ".env", ".toml", ".gradle", ".sh"}

    def repo_scan(self, d):
        """Klasörü gizli dosya / dev dosya / içerikte anahtar için tarar. Hariç tutulacakları döner."""
        d = Path(d)
        sens, secret_in, big, heavy = [], [], [], []
        n = 0
        for root, dirs, files in os.walk(str(d)):
            rel_root = Path(root).relative_to(d)
            keep = []
            for x in dirs:
                if x == ".git":
                    continue
                if x.lower() in self.HEAVY_DIRS:
                    heavy.append(str((rel_root / x).as_posix()) + "/")
                else:
                    keep.append(x)
            dirs[:] = keep
            for f in files:
                n += 1
                if n > 20000:
                    break
                fp = Path(root) / f
                rel = (rel_root / f).as_posix()
                try:
                    sz = fp.stat().st_size
                except OSError:
                    continue
                if self.SENS_RE.search(rel):
                    sens.append(rel)
                    continue
                if sz > 90 * 1048576:
                    big.append(rel)
                    continue
                if fp.suffix.lower() in self.TEXT_EXT and sz <= 300000:
                    try:
                        txt = fp.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    if any(rx.search(txt) for rx, _ in SECRET_PATTERNS):
                        secret_in.append(rel)
            if n > 20000:
                break
        return {"sens": sens, "secret_in": secret_in, "big": big, "heavy": heavy, "files": n}

    def apply_ignore(self, d, scan):
        d = Path(d)
        lines = []
        for k in ("sens", "secret_in", "big"):
            lines += ["/" + x for x in scan[k]]
        lines += ["/" + x for x in scan["heavy"]]
        gi = d / ".gitignore"
        cur = gi.read_text(encoding="utf-8", errors="ignore") if gi.exists() else ""
        have = set(l.strip() for l in cur.splitlines())
        add = [l for l in lines if l not in have]
        if add:
            with open(gi, "a", encoding="utf-8", newline="\n") as f:
                if cur and not cur.endswith("\n"):
                    f.write("\n")
                f.write("# Köprü botu tarafından eklendi (gizli / büyük dosyalar)\n" + "\n".join(add) + "\n")
        return len(add)

    def scan_report(self, scan):
        def blk(title, items):
            return ("%s (%d):\n" % (title, len(items)) + "\n".join("  • " + x for x in items[:8]) +
                    ("\n  … +%d" % (len(items) - 8) if len(items) > 8 else "") + "\n") if items else ""
        t = "🔎 Güvenlik taraması: %d dosya bakıldı\n" % scan["files"]
        t += blk("🔑 Gizli dosya (yüklenmeyecek)", scan["sens"])
        t += blk("⚠️ İçinde anahtar/token var (yüklenmeyecek)", scan["secret_in"])
        t += blk("📦 90 MB üstü (yüklenmeyecek)", scan["big"])
        t += blk("🗑 Derleme/önbellek klasörü (yüklenmeyecek)", scan["heavy"])
        if not (scan["sens"] or scan["secret_in"] or scan["big"] or scan["heavy"]):
            t += "Sorun bulmadım ✅\n"
        return t

    def dp_valid(self):
        if self.dp and time.time() - self.dp.get("ts", 0) < 1800:
            return self.dp
        self.dp = None
        return None

    def cmd_depoyeni(self, chat, arg=""):
        a = (arg or "").strip().strip('"')
        d = Path(a) if a else self.proj_dir()[0]
        if not d:
            self.tg.send_text(chat, "📂 Önce klasörü seç. Aşağıdan klasörü bul, içinde \"🚀 Bu klasörü GitHub'a gönder\" düğmesine bas.")
            self.tg.send_text(chat, *self.browse_view(None))
            return
        self.dp_start(chat, d)

    def dp_start(self, chat, d):
        d = Path(d)
        if not d.is_dir() or not path_allowed(d, self.roots):
            self.tg.send_text(chat, "⚠️ Bu klasörü kullanamam (yok, izinli yerlerin dışında ya da korumalı).")
            return
        active, accs, code, out = self.gh_accounts()
        if not accs:
            self.tg.send_text(chat, "🐙 GitHub girişi yok. Bilgisayarda bir kere `gh auth login` yap, sonra tekrar dene.\n" + short(out, 300))
            return
        c, o = self.run_cli(["git", "remote", "get-url", "origin"], cwd=d, timeout=15)
        if (d / ".git").exists() and c == 0 and o.strip():
            self.tg.send_text(chat, "Bu klasör zaten bir depoya bağlı:\n%s\nKodu güncellemek için /kodgonder yaz." % o.strip())
            return
        default = re.sub(r"[^A-Za-z0-9._-]+", "-", d.name).strip("-.") or "yeni-proje"
        self.dp = {"dir": str(d), "ts": time.time(), "await": "name", "mode": "new", "default": default}
        kb = [[{"text": "✅ " + default, "callback_data": "dp:name"}], [{"text": "✖️ Vazgeç", "callback_data": "dp:no"}]]
        self.tg.send_text(chat, "🚀 Yeni GitHub deposu\n%s\nKlasör: %s\nHesap: %s\n\nDepo adı ne olsun? Aşağıdan seç ya da yeni adı yaz (harf, rakam, - _ .)." % (LINE, d, active or "?"), kb)

    def dp_name(self, chat, text):
        dp = self.dp_valid()
        if not dp:
            return
        nm = text.strip()
        if not re.match(r"^[A-Za-z0-9._-]{1,80}$", nm):
            self.tg.send_text(chat, "⚠️ Ad geçersiz. Sadece harf, rakam ve - _ . kullan (boşluk, Türkçe harf yok). Tekrar yaz.")
            return
        dp["name"] = nm
        dp["await"] = None
        dp["ts"] = time.time()
        kb = [[{"text": "🔒 Özel (önerilen)", "callback_data": "dp:priv"}], [{"text": "🌍 Herkese açık", "callback_data": "dp:pub"}],
              [{"text": "✖️ Vazgeç", "callback_data": "dp:no"}]]
        self.tg.send_text(chat, "Depo adı: %s\nKim görebilsin?" % nm, kb)

    def dp_scan_and_ask(self, chat):
        dp = self.dp_valid()
        if not dp:
            self.tg.send_text(chat, "Süre doldu. /depoyeni ile yeniden başla.")
            return
        self.tg.send_text(chat, "🔎 Klasör taranıyor…")
        def work():
            try:
                sc = self.repo_scan(dp["dir"])
            except Exception as e:
                self.tg.send_text(chat, "⚠️ Tarama hatası: %s" % e)
                return
            dp["scan"] = sc
            dp["ts"] = time.time()
            who = self.gh_accounts()[0]
            if dp.get("mode") == "push":
                head = "📤 Kod güncelleme\n%s\nKlasör: %s\nMesaj: %s\n\n" % (LINE, dp["dir"], dp.get("msg"))
            else:
                head = "🚀 Yeni depo\n%s\nAd: %s/%s\nGörünürlük: %s\nKlasör: %s\n\n" % (LINE, who or "?", dp.get("name"), "🌍 HERKESE AÇIK" if dp.get("vis") == "public" else "🔒 özel", dp["dir"])
            kb = [[{"text": "✅ Evet, gönder", "callback_data": "dp:go"}], [{"text": "✖️ Vazgeç", "callback_data": "dp:no"}]]
            self.tg.send_text(chat, head + self.scan_report(sc) + "\nBulunanları .gitignore'a ekleyip dışarıda bırakırım. Gönderelim mi?", kb)
        threading.Thread(target=work, daemon=True).start()

    def dp_go(self, chat):
        dp = self.dp_valid()
        if not dp or "scan" not in dp:
            self.tg.send_text(chat, "Süre doldu. Baştan başla (/depoyeni ya da /kodgonder).")
            return
        if self.dp_busy:
            self.tg.send_text(chat, "⏳ Zaten bir gönderim sürüyor.")
            return
        self.dp_busy = True
        self.dp = None
        self.tg.send_text(chat, "⬆️ Gönderiliyor… (büyük projede birkaç dakika sürebilir)")
        def work():
            d = Path(dp["dir"])
            try:
                self.apply_ignore(d, dp["scan"])
                if not (d / ".git").exists():
                    c, o = self.run_cli(["git", "init", "-b", "main"], cwd=d, timeout=60)
                    if c != 0:
                        self.tg.send_text(chat, "⚠️ git başlatılamadı:\n" + short(o, 500))
                        return
                self.run_cli(["git", "add", "-A"], cwd=d, timeout=300)
                c, o = self.run_cli(["git", "commit", "-m", dp.get("msg") or "İlk sürüm"], cwd=d, timeout=120)
                if c != 0 and "nothing to commit" not in o.lower() and "nothing added" not in o.lower():
                    hint = "\n\nGit kimliğin yok olabilir. Bilgisayarda bir kere:\ngit config --global user.name \"Adın\"\ngit config --global user.email \"mailin\"" if "identity" in o.lower() or "user.email" in o.lower() else ""
                    self.tg.send_text(chat, "⚠️ Kayıt (commit) oluşturulamadı:\n" + short(o, 600) + hint)
                    return
                if dp.get("mode") == "push":
                    c, o = self.run_cli(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push"], cwd=d, timeout=900)
                    if c != 0:
                        c, o = self.run_cli(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push", "-u", "origin", "HEAD"], cwd=d, timeout=900)
                    self.tg.send_text(chat, ("✅ Kod gönderildi.\n" + short(o, 500)) if c == 0 else "⚠️ Gönderilemedi:\n" + short(o, 800))
                    return
                vis = "--public" if dp.get("vis") == "public" else "--private"
                c, o = self.run_cli(["gh", "repo", "create", dp["name"], vis, "--source=.", "--remote=origin", "--push"], cwd=d, timeout=1200)
                if c == 0:
                    m = re.search(r"https://github\.com/\S+", o)
                    url = m.group(0) if m else "(GitHub'da deponu kontrol et)"
                    sel = self.selected_valid()
                    mp = self.dep_map_load()
                    mp[sel[0] if sel else dp["name"]] = {"depo": "%s/%s" % (self.gh_accounts()[0] or "", dp["name"]), "dir": str(d)}
                    self.dep_map_save(mp)
                    self.tg.send_text(chat, "🎉 Depo hazır\n%s\n%s\n(%s)" % (LINE, url, "herkese açık" if vis == "--public" else "özel"),
                                      [[{"text": "🌐 Depoyu aç", "url": url}]] if m else None)
                elif "already exists" in o.lower() or "zaten" in o.lower():
                    acct = self.gh_accounts()[0] or ""
                    full = "%s/%s" % (acct, dp["name"])
                    c2, o2 = self.run_cli(["gh", "repo", "view", full, "--json", "url,visibility,isEmpty,defaultBranchRef"], timeout=40)
                    info = {}
                    try:
                        info = json.loads(o2)
                    except Exception:
                        pass
                    br = ((info.get("defaultBranchRef") or {}).get("name")) or "main"
                    self.dp = {"dir": str(d), "ts": time.time(), "mode": "link", "repo": full, "url": info.get("url") or "https://github.com/" + full,
                               "branch": br, "scan": dp["scan"], "msg": dp.get("msg") or "Güncelleme", "name": dp["name"]}
                    self.tg.send_text(chat, "ℹ️ '%s' adında bir depo ZATEN VAR (%s, %s).\n\nYeni depo açamam. Seçenekler:\n"
                                            "• Mevcut depoya bağlayıp gönder (üzerine yazmam; çakışma olursa dururum)\n"
                                            "• Başka bir ad ver" % (full, info.get("visibility", "?").lower(), "boş" if info.get("isEmpty") else "içinde dosya var"),
                                      [[{"text": "🔗 Mevcut depoya bağla ve gönder", "callback_data": "dp:link"}],
                                       [{"text": "✏️ Başka ad ver", "callback_data": "dp:rename"}], [{"text": "✖️ Vazgeç", "callback_data": "dp:no"}]])
                else:
                    self.tg.send_text(chat, "⚠️ Depo oluşturulamadı:\n" + short(o, 800))
            except Exception as e:
                self.tg.send_text(chat, "⚠️ Hata: %s" % e)
            finally:
                self.dp_busy = False
        threading.Thread(target=work, daemon=True).start()

    def dp_link_go(self, chat):
        dp = self.dp_valid()
        if not dp or dp.get("mode") != "link" or self.dp_busy:
            self.tg.send_text(chat, "Süre doldu ya da işlem sürüyor. /depoyeni ile yeniden başla.")
            return
        self.dp_busy = True
        self.dp = None
        self.tg.send_text(chat, "🔗 Mevcut depoya bağlanıp gönderiliyor…")
        def work():
            d = Path(dp["dir"])
            try:
                url = dp["url"] if dp["url"].endswith(".git") else dp["url"] + ".git"
                c, o = self.run_cli(["git", "remote", "add", "origin", url], cwd=d, timeout=30)
                if c != 0:
                    self.run_cli(["git", "remote", "set-url", "origin", url], cwd=d, timeout=30)
                c, o = self.run_cli(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push", "-u", "origin", "HEAD:" + dp["branch"]],
                                    cwd=d, timeout=1200)
                if c == 0:
                    sel = self.selected_valid()
                    mp = self.dep_map_load()
                    mp[sel[0] if sel else dp["name"]] = {"depo": dp["repo"], "dir": str(d)}
                    self.dep_map_save(mp)
                    self.tg.send_text(chat, "🎉 Gönderildi\n%s\n%s (dal: %s)" % (LINE, dp["url"], dp["branch"]), [[{"text": "🌐 Depoyu aç", "url": dp["url"]}]])
                else:
                    self.tg.send_text(chat, "⚠️ Gönderilemedi. Depoda bilgisayarında olmayan kayıtlar olabilir; zorla üzerine YAZMADIM.\n" + short(o, 700) +
                                      "\n\nÇözüm: başka bir depo adıyla yeni depo aç (/depoyeni) ya da çakışmayı bilgisayarda birleştir.")
            except Exception as e:
                self.tg.send_text(chat, "⚠️ Hata: %s" % e)
            finally:
                self.dp_busy = False
        threading.Thread(target=work, daemon=True).start()

    def dep_map_load(self):
        try:
            return json.loads((BASE / "depolar.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def dep_map_save(self, m):
        try:
            (BASE / "depolar.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def cmd_kodgonder(self, chat, arg=""):
        d, name = self.proj_dir()
        if not d:
            self.tg.send_text(chat, "Önce klasörü bulunabilen bir proje seç (/menu ya da /klasor → 📌).")
            return
        c, o = self.run_cli(["git", "remote", "get-url", "origin"], cwd=d, timeout=15)
        if not (d / ".git").exists() or c != 0:
            self.tg.send_text(chat, "Bu klasör henüz bir GitHub deposuna bağlı değil. Önce /depoyeni.")
            return
        msg = (arg or "").strip() or ("Güncelleme " + time.strftime("%d.%m.%Y %H:%M"))
        self.dp = {"dir": str(d), "ts": time.time(), "mode": "push", "msg": msg}
        self.dp_scan_and_ask(chat)

    # ------------------------------------------------------------------ bildirim ayarları
    def mute_load(self):
        try:
            return set(json.loads((BASE / "sustur.json").read_text(encoding="utf-8")))
        except Exception:
            return set()

    def mute_save(self):
        try:
            (BASE / "sustur.json").write_text(json.dumps(sorted(self.muted), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def notif_on(self):
        return str(self.cfg.get("BILDIRIM", "1")) != "0"

    def notif_min(self):
        try:
            return max(0, int(self.cfg.get("BILDIRIM_MIN_SN", "30")))
        except Exception:
            return 30

    def notif_gap(self):
        try:
            return max(0, int(self.cfg.get("BILDIRIM_ARA_DK", "10")))
        except Exception:
            return 10

    def notif_menu(self):
        class _C:
            def __init__(c, o):
                c.o, c.out = o, None
        cap = []
        orig = self.tg.send_text
        self.tg.send_text = lambda ch, t, m=None: cap.append((t, m))
        try:
            self.cmd_bildirim(0, "")
        finally:
            self.tg.send_text = orig
        return cap[0]

    def cmd_bildirim(self, chat, arg=""):
        a = (arg or "").strip().lower().split()
        if a and a[0] in ("kapat", "off"):
            save_env_value("BILDIRIM", "0")
            self.cfg["BILDIRIM"] = "0"
            self.tg.send_text(chat, "🔕 'İş bitti' bildirimleri kapatıldı. (Onay, limit ve takılma uyarıları yine gelir.) Açmak: /bildirim ac")
            return
        if a and a[0] in ("ac", "aç", "on"):
            save_env_value("BILDIRIM", "1")
            self.cfg["BILDIRIM"] = "1"
            self.tg.send_text(chat, "🔔 'İş bitti' bildirimleri açıldı.")
            return
        if len(a) == 2 and a[0] in ("sure", "süre") and a[1].isdigit():
            save_env_value("BILDIRIM_MIN_SN", a[1])
            self.cfg["BILDIRIM_MIN_SN"] = a[1]
            self.tg.send_text(chat, "⏱ %s saniyeden kısa işler için bildirim göndermem (sen bota yazdıysan her zaman gelir)." % a[1])
            return
        if len(a) == 2 and a[0] == "ara" and a[1].isdigit():
            save_env_value("BILDIRIM_ARA_DK", a[1])
            self.cfg["BILDIRIM_ARA_DK"] = a[1]
            self.tg.send_text(chat, "⏳ Aynı sohbetten %s dakika içinde ikinci kısa bildirim göndermem." % a[1])
            return
        if a and a[0] in ("sustur", "sustur-sil") and self.muted:
            self.muted = set()
            self.mute_save()
            self.tg.send_text(chat, "🔔 Tüm susturulan sohbetler açıldı.")
            return
        kb = [[{"text": "🔕 Hepsini kapat" if self.notif_on() else "🔔 Aç", "callback_data": "bn:" + ("off" if self.notif_on() else "on")}],
              [{"text": "⏱ Kısa işleri yoksay: 30 sn", "callback_data": "bn:s30"}, {"text": "60 sn", "callback_data": "bn:s60"}, {"text": "120 sn", "callback_data": "bn:s120"}]]
        if self.muted:
            kb.append([{"text": "🔔 Susturulanları aç (%d)" % len(self.muted), "callback_data": "bn:unmute"}])
        txt = ("🔔 Bildirim ayarları\n%s\n'İş bitti' bildirimi: %s\nKısa işleri yoksay: %d sn\nAynı sohbette tekrar arası: %d dk\nSusturulan sohbet: %d\n\n"
               "Her bildirimin altındaki 🔕 düğmesi o sohbeti susturur.\nKomutlar: /bildirim kapat · ac · sure 60 · ara 15") % (
            LINE, "açık" if self.notif_on() else "KAPALI", self.notif_min(), self.notif_gap(), len(self.muted))
        self.tg.send_text(chat, txt, kb)

    def cmd_ekran(self, chat):
        png = self.with_cdp(self.cdp.screenshot)
        self.tg.send_photo(chat, "ekran.png", png, "Antigravity ekranı")

    def cmd_teshis(self, chat):
        d = self.with_cdp(lambda: self.cdp.agk("diag()"))
        raw = json.dumps(d, ensure_ascii=False, indent=2)
        (BASE / "teshis.json").write_text(raw, encoding="utf-8")
        self.tg.send_document(chat, "teshis.json", raw.encode("utf-8"), "Arayüz teşhis dosyası")

    def cmd_pc(self, chat):
        L = ["🖥 Bilgisayar durumu", LINE]
        run = [r for r in self.rows if r.get("kind") == "conv" and r.get("running")]
        L.append("%s Antigravity: %s" % ("🟢" if self.cdp_ok else "🔴", "bağlı" if self.cdp_ok else "bağlantı yok"))
        L.append("⏳ Çalışan iş: %d" % len(run))
        idle = idle_seconds()
        L.append("👤 Sen: %s" % (("başında değilsin (%s boşta)" % fmt_dur(idle)) if self.user_away() else "bilgisayarın başındasın"))
        L.append("")
        try:
            du = shutil.disk_usage(str(Path.home().anchor or "/"))
            fr = (du.total - du.free) / float(du.total)
            L.append("💽 Disk   %s %%%d\n      %s boş / %s" % (bar(fr), fr * 100, human_size(du.free), human_size(du.total)))
        except Exception:
            pass
        if IS_WIN:
            try:
                r = ps("$o=Get-CimInstance Win32_OperatingSystem; '{0}|{1}|{2}' -f $o.TotalVisibleMemorySize,$o.FreePhysicalMemory,[int]((Get-Date)-$o.LastBootUpTime).TotalSeconds", 20)
                tot, free, up = [float(x) for x in r.strip().split("|")]
                fr = (tot - free) / tot
                L.append("🧠 Bellek %s %%%d\n      %.1f / %.1f GB" % (bar(fr), fr * 100, (tot - free) / 1048576, tot / 1048576))
                cpu = ps("(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average", 20)
                if cpu:
                    c = float(cpu) / 100.0
                    L.append("⚙️ İşlemci %s %%%d" % (bar(c), c * 100))
                L.append("")
                L.append("⏱ Bilgisayar açık: %s" % fmt_dur(up))
                bat = ps("$b=Get-CimInstance Win32_Battery; if($b){'{0}' -f $b.EstimatedChargeRemaining}", 20)
                if bat:
                    L.append("🔋 Pil: %%%s" % bat)
            except Exception:
                pass
        L.append("🌉 Köprü çalışma süresi: %s" % fmt_dur(time.time() - self.started))
        L.append("😴 Uyku engeli: %s   🛡 Güvenli mod: %s" % ("açık" if self.keep_awake_on else "kapalı", "açık" if self.safe_mode else "kapalı"))
        try:
            size = sum(f.stat().st_size for f in self.gelen.rglob("*") if f.is_file()) if self.gelen.exists() else 0
            L.append("📥 'gelen' klasörü: %s" % human_size(size))
        except Exception:
            pass
        L.append("⏰ Zamanlanmış mesaj: %d" % len(self.sched))
        kb = [[{"text": "🔄 Yenile", "callback_data": "tl:pc"}, {"text": "📸 Ekran", "callback_data": "tl:ekran"}],
              [{"text": "🔒 Ekranı kilitle", "callback_data": "tl:kilitle"}, {"text": "♻️ Antigravity'yi yeniden aç", "callback_data": "tl:yenidenbaslat"}]]
        self.tg.send_text(chat, "\n".join(L), kb)

    def cmd_kilitle(self, chat):
        if not IS_WIN:
            self.tg.send_text(chat, "Bu komut yalnızca Windows'ta çalışır.")
            return
        kb = [[{"text": "✅ Evet, kilitle", "callback_data": "tl:kilit_ok"}, {"text": "✖️ Vazgeç", "callback_data": "cancel"}]]
        self.tg.send_text(chat, "🔒 Windows ekranı kilitlenecek. Köprü ve Antigravity çalışmaya devam eder.\nKilitleyeyim mi?", kb)

    def cmd_araclar(self, chat):
        kb = [[{"text": "➕ Yeni sohbet", "callback_data": "nn"}, {"text": "🤖 Model", "callback_data": "tl:model"}],
              [{"text": "🔁 Tekrar dene", "callback_data": "tl:tekrar"}, {"text": "⏹ Durdur", "callback_data": "tl:dur"}],
              [{"text": "📸 Ekran", "callback_data": "tl:ekran"}, {"text": "📤 Dosya al", "callback_data": "tl:dosya"}],
              [{"text": "📂 Klasörler", "callback_data": "tl:klasor"}, {"text": "🌿 Git durumu", "callback_data": "tl:git"}],
              [{"text": "🐙 GitHub hesabı", "callback_data": "tl:hesap"}, {"text": "🚀 Yeni depo", "callback_data": "tl:depoyeni"}],
              [{"text": "📲 WhatsApp", "callback_data": "tl:whatsapp"}, {"text": "⏰ Zamanlı", "callback_data": "tl:zamanlar"}],
              [{"text": "🌙 Sessiz saat", "callback_data": "tl:sessiz"}, {"text": "🩺 Sağlık", "callback_data": "tl:saglik"}],
              [{"text": "🖥 Bilgisayar", "callback_data": "tl:pc"}, {"text": "🔒 Kilitle", "callback_data": "tl:kilitle"}],
              [{"text": "🚀 GitHub'a yayınla", "callback_data": "tl:yayinla"}, {"text": "🔄 Güncelle", "callback_data": "tl:guncelle"}],
              [{"text": "⚠️ Hatalar", "callback_data": "tl:hata"}],
              [{"text": "🧪 Test çalıştır", "callback_data": "tl:test"}, {"text": "📋 Günlük özet", "callback_data": "tl:ozet"}, {"text": "🔔 Bildirimler", "callback_data": "tl:bildirim"}],
              [{"text": "🆘 Teşhis", "callback_data": "tl:teshis"}],
              [{"text": "📖 Yardım", "callback_data": "tl:start"}]]
        self.tg.send_text(chat, "🧰 Araçlar\n" + LINE + "\nBir işlem seç:\n"
                                "🤖 Model — seçili sohbetin modelini değiştirir\n"
                                "🔁 Tekrar dene — hata veren işi yeniden başlatır\n"
                                "📤 Dosya al — PC'den dosya çeker\n"
                                "🌙 Sessiz saat — gece bildirimlerini biriktirir", kb)

    def cmd_yenidenbaslat(self, chat):
        if not IS_WIN:
            self.tg.send_text(chat, "Bu komut yalnızca Windows'ta çalışır.")
            return
        kb = [[{"text": "✅ Evet, kapat ve aç", "callback_data": "rs"}, {"text": "✖️ Vazgeç", "callback_data": "dno"}]]
        n = len([r for r in self.rows if r.get("kind") == "conv" and r.get("running")])
        self.tg.send_text(chat, "⚠️ Antigravity kapatılıp köprü portuyla yeniden açılacak.\n"
                                "Çalışan iş sayısı: %d (hepsi yarıda kalır!)\nEmin misin?" % n, kb)

    def restart_antigravity(self):
        kill_antigravity()
        time.sleep(3)
        exe = pick_exe(find_antigravity_exes(self.cfg), self.cfg)
        if not exe:
            raise RuntimeError("Antigravity'nin yolunu bulamadım")
        launch_antigravity(exe, self.cfg)

    def cmd_saglik(self, chat):
        with self.lock:
            h = self.with_cdp(lambda: self.cdp.agk("health()")) or {}
        ok = lambda v: "✅" if v else "⚠️"
        L = ["🩺 Sağlık kontrolü (köprü %s)" % VERSION, "",
             "%s Sol liste: %s proje, %s sohbet" % (ok(h.get("folders") and h.get("convs")), h.get("folders"), h.get("convs")),
             "%s Çalışan iş algılama: %s çalışıyor görünüyor" % ("✅", h.get("running")),
             "%s Liste kaydırma: %s" % (ok(h.get("scroller")), "var" if h.get("scroller") else "yok (liste kısa ya da bulunamadı)"),
             "%s Mesaj kutusu: %s" % (ok(h.get("composer")), "bulundu" if h.get("composer") else "bulunamadı (bir sohbet açık olmalı)"),
             "%s Dosya ekleme alanı: %s" % (ok(h.get("fileInputs")), "var" if h.get("fileInputs") else "yok (ek yolu yöntemleri dener)"),
             "%s Model seçici: %s" % (ok(h.get("model")), h.get("model") or "bulunamadı"),
             "%s Durdur düğmesi: %s" % ("ℹ️", "görünüyor" if h.get("stop") else "görünmüyor (iş çalışmıyorsa normal)"),
             "%s Tekrar dene düğmesi: %s" % ("ℹ️", "var" if h.get("retry") else "yok (hata yoksa normal)"),
             "%s Onay bekleyen düğme: %s" % ("ℹ️", h.get("approvals")),
             "", "Açık sohbet: %s" % short(h.get("header") or "-", 100),
             "Pencere: %s" % (h.get("size"),),
             "WhatsApp kişileri: %d" % len(self.wa.contacts()),
             "Sessiz saat: %s · Oto onay: %s · Güvenli mod: %s" % (self.cfg.get("SESSIZ") or "kapalı", "açık" if self.cfg.get("OTO_ONAY") else "kapalı",
                                                              "açık" if self.safe_mode else "kapalı")]
        self.tg.send_text(chat, "\n".join(L))
        self.dump_diag()

    def cmd_model(self, chat):
        sel = self.selected_valid()
        if not sel:
            self.tg.send_text(chat, *self.projects_menu("Model değiştirmek için önce bir sohbet seç."))
            return
        cid = hid(*sel)
        self.convs[cid] = sel
        with self.lock:
            self.open_conv(*sel)

            def f():
                b = self.cdp.agk("modelBtn()")
                if not b:
                    raise UIError("Model seçici bulunamadı. /saglik ile kontrol et.")
                self.cdp.click(b["x"], b["y"])
                time.sleep(0.9)
                opts = self.cdp.agk("modelOptions()") or []
                self.cdp.press_key("Escape", "Escape", 27)
                time.sleep(0.3)
                return b["text"], [o["text"] for o in opts]
            cur, opts = self.with_cdp(f)
        if not opts:
            self.tg.send_text(chat, "Şu anki model: %s\nSeçenek listesini okuyamadım (/saglik)." % cur)
            return
        self.model_opts[cid] = opts
        kb = [[{"text": ("✅ " if o == cur else "") + short(o, 40), "callback_data": "m:%s:%d" % (cid, i)}] for i, o in enumerate(opts)]
        self.tg.send_text(chat, "🤖 Model\n💬 %s\nŞu anki: %s\n\nDeğiştirmek istediğine dokun:" % (sel[1], cur), kb)

    def do_model(self, chat, cid, idx):
        pc = self.convs.get(cid)
        opts = self.model_opts.get(cid) or []
        if not pc or idx >= len(opts):
            self.tg.send_text(chat, "Bu seçimin süresi dolmuş. /model yaz.")
            return
        want = opts[idx]
        with self.lock:
            self.open_conv(*pc)

            def f():
                b = self.cdp.agk("modelBtn()")
                if not b:
                    raise UIError("Model seçici bulunamadı.")
                self.cdp.click(b["x"], b["y"])
                time.sleep(0.9)
                pos = self.cdp.agk("optionPos(%s)" % json.dumps(want))
                if not pos:
                    raise UIError("'%s' seçeneği listede görünmüyor." % want)
                self.cdp.click(pos["x"], pos["y"])
                time.sleep(1.0)
                nb = self.cdp.agk("modelBtn()")
                return nb["text"] if nb else ""
            now = self.with_cdp(f)
        if now and (norm(want) in norm(now) or norm(now) in norm(want)):
            self.tg.send_text(chat, "✅ Model değişti: %s\n💬 %s" % (now, pc[1]))
        else:
            self.tg.send_text(chat, "⚠️ Tıkladım ama modeli doğrulayamadım (şu an: %s). /model ile kontrol et." % (now or "?"))

    # ------------------------------------------------------------------ Yeni sohbet sihirbazı
    WIZ_TTL = 1800

    def wiz_ok(self):
        w = getattr(self, "wiz", None)
        if w and time.time() - w["t"] < self.WIZ_TTL:
            return w
        self.wiz = None
        return None

    def wiz_read(self, kind):
        """Yeni sohbet sayfasını açıp bir denetimin şu anki değerini ve açılır listesini okur."""
        with self.lock:
            def f():
                if not self.cdp.agk("navNewClick()"):
                    nav = self.cdp.agk("navNewPos()")
                    if not nav:
                        raise UIError("Soldaki 'New Conversation' düğmesini bulamadım")
                    self.cdp.click(nav["x"], nav["y"])
                time.sleep(1.6)
                b = self.cdp.agk("ctl(%s)" % json.dumps(kind))
                if not b:
                    raise UIError("'%s' denetimi yeni sohbet sayfasında bulunamadı" % kind)
                self.cdp.click(b["x"], b["y"])
                time.sleep(0.9)
                opts = [o["text"] for o in (self.cdp.agk("menuAll()") or [])]
                self.cdp.press_key("Escape", "Escape", 27)
                time.sleep(0.3)
                return b["text"], opts
            return self.with_cdp(f)

    @staticmethod
    def wiz_env_tr(o):
        l = o.lower()
        if "worktree" in l:
            return "🌿 Yeni çalışma ağacı (New Worktree)"
        if "local" in l:
            return "💻 Bu bilgisayarda (Local)"
        return o

    def wiz_start(self, chat):
        self.newproj = None
        self.selected = None
        self.wiz = {"t": time.time(), "model": None, "env": None, "proj": None, "await": None,
                    "name": None, "base": None, "mopts": [], "eopts": [], "popts": []}
        try:
            cur, opts = self.wiz_read("model")
        except (UIError, CDPError) as e:
            self.wiz["mopts"] = []
            self.tg.send_text(chat, "⚠️ Model listesini okuyamadım (%s). Model aşamasını atlıyorum." % e)
            self.wiz_step_env(chat)
            return
        opts = [o for o in opts if MODEL_PY.search(o)]
        self.wiz["mopts"] = opts
        self.wiz["mcur"] = cur
        self.tg.send_text(chat, "🆕 Yeni sohbet — 1/4  Model\n\nŞu anki model: %s\nModeli değiştirmek ister misin?" % cur,
                          [[{"text": "🔄 Evet, değiştir", "callback_data": "wz:mq"},
                            {"text": "➡️ Hayır, böyle kalsın", "callback_data": "wz:env"}],
                           [{"text": "✖️ İptal", "callback_data": "cancel"}]])

    def wiz_step_env(self, chat):
        w = self.wiz_ok()
        if not w:
            return
        try:
            cur, opts = self.wiz_read("env")
        except (UIError, CDPError):
            w["eopts"] = []
            self.wiz_step_proj(chat)
            return
        w["eopts"] = [o for o in opts if re.search(r"local|worktree|remote", o, re.I)]
        w["ecur"] = cur
        self.tg.send_text(chat, "🆕 Yeni sohbet — 2/4  Çalışma yeri\n\nŞu an: %s\n"
                                "Local = bu bilgisayarda çalışır. New Worktree = projenin ayrı bir kopyasında çalışır.\n"
                                "Değiştirmek ister misin?" % self.wiz_env_tr(cur),
                          [[{"text": "🔄 Evet, değiştir", "callback_data": "wz:eq"},
                            {"text": "➡️ Hayır, böyle kalsın", "callback_data": "wz:proj"}],
                           [{"text": "✖️ İptal", "callback_data": "cancel"}]])

    def wiz_step_proj(self, chat):
        w = self.wiz_ok()
        if not w:
            return
        try:
            cur, opts = self.wiz_read("proj")
        except (UIError, CDPError):
            cur, opts = "", []
        w["pcur"] = cur
        w["popts"] = [o for o in opts if o.lower() not in ("new project", "quick start", "no project") and "search" not in o.lower()]
        kb = []
        if cur:
            kb.append([{"text": "📁 Şu anki projede: %s" % short(cur, 28), "callback_data": "wz:pc"}])
        kb.append([{"text": "📂 Başka mevcut proje seç", "callback_data": "wz:pl"}])
        kb.append([{"text": "🚫 Projesiz, sadece sohbet (No Project)", "callback_data": "wz:pn"}])
        kb.append([{"text": "🆕 Yeni proje (yeni klasör oluştur)", "callback_data": "wz:pw"}])
        kb.append([{"text": "✖️ İptal", "callback_data": "cancel"}])
        self.tg.send_text(chat, "🆕 Yeni sohbet — 3/4  Proje\n\nBu sohbet nerede olsun?\n"
                                "• Şu anki proje: Antigravity'de seçili görünen proje (%s)\n"
                                "• Projesiz: klasörsüz, sadece sohbet\n"
                                "• Yeni proje: sana sorup yeni bir klasör açarım" % (cur or "okunamadı"), kb)

    def wiz_locations(self):
        home = Path.home()
        out = [("🖥 Masaüstü", home / "Desktop"), ("📄 Belgelerim", home / "Documents"), ("⬇️ İndirilenler", home / "Downloads")]
        for base in (home / "Desktop", home / "Documents", home):
            for nm in ("Uygulamalarım", "Projelerim", "Uygulamalarim", "Projelerim"):
                d = base / nm
                if d.is_dir() and all(d != x[1] for x in out):
                    out.append(("📦 %s (%s)" % (nm, base.name), d))
        return out

    def wiz_callback(self, chat, arg):
        w = self.wiz_ok()
        if not w:
            self.tg.send_text(chat, "Sihirbazın süresi dolmuş. Projeler menüsünden yeniden başlat.", None)
            return
        w["t"] = time.time()
        if arg == "mq":
            kb = [[{"text": short(o, 40), "callback_data": "wz:m%d" % i}] for i, o in enumerate(w["mopts"])]
            kb.append([{"text": "⬅️ Geri", "callback_data": "wz:env"}])
            self.tg.send_text(chat, "Hangi model?", kb if w["mopts"] else [[{"text": "➡️ Devam", "callback_data": "wz:env"}]])
        elif re.match(r"^m\d+$", arg):
            w["model"] = w["mopts"][int(arg[1:])]
            self.tg.send_text(chat, "✅ Model: %s" % w["model"])
            self.wiz_step_env(chat)
        elif arg == "env":
            self.wiz_step_env(chat)
        elif arg == "eq":
            kb = [[{"text": self.wiz_env_tr(o), "callback_data": "wz:e%d" % i}] for i, o in enumerate(w["eopts"])]
            kb.append([{"text": "⬅️ Geri", "callback_data": "wz:proj"}])
            self.tg.send_text(chat, "Çalışma yeri hangisi olsun?", kb)
        elif re.match(r"^e\d+$", arg):
            w["env"] = w["eopts"][int(arg[1:])]
            self.tg.send_text(chat, "✅ Çalışma yeri: %s" % self.wiz_env_tr(w["env"]))
            self.wiz_step_proj(chat)
        elif arg == "proj":
            self.wiz_step_proj(chat)
        elif arg == "pc":
            w["proj"] = ("keep", w.get("pcur", ""))
            self.wiz_step_msg(chat)
        elif arg == "pn":
            w["proj"] = ("none", "")
            self.wiz_step_msg(chat)
        elif arg == "pl":
            kb = [[{"text": "📁 " + short(o, 40), "callback_data": "wz:q%d" % i}] for i, o in enumerate(w["popts"][:20])]
            kb.append([{"text": "⬅️ Geri", "callback_data": "wz:proj"}])
            self.tg.send_text(chat, "Hangi proje?" if w["popts"] else "Proje listesini okuyamadım.", kb)
        elif re.match(r"^q\d+$", arg):
            w["proj"] = ("existing", w["popts"][int(arg[1:])])
            self.wiz_step_msg(chat)
        elif arg == "pw":
            w["await"] = "name"
            self.tg.send_text(chat, "🆕 Yeni proje\n\nKlasöre verilecek ismi yaz (ör: Deneme):",
                              [[{"text": "✖️ İptal", "callback_data": "cancel"}]])
        elif re.match(r"^l\d+$", arg):
            locs = self.wiz_locations()
            name, base = locs[int(arg[1:])]
            w["base"] = str(base)
            target = Path(w["base"]) / w["name"]
            w["proj"] = ("new", str(target))
            self.tg.send_text(chat, "📁 Klasör şuraya oluşturulacak:\n%s\n\nOnaylıyor musun?" % target,
                              [[{"text": "✅ Onayla", "callback_data": "wz:mk"},
                                {"text": "✖️ İptal", "callback_data": "cancel"}]])
        elif arg == "mk":
            kind, target = w["proj"]
            self.tg.send_text(chat, "✅ Tamam. Klasörü (%s) Antigravity kendisi oluşturacak; yolu isteğinin başına ekleyeceğim." % target)
            self.wiz_step_msg(chat)

    def wiz_name(self, chat, text):
        w = self.wiz_ok()
        name = (text or "").strip()
        if not name or re.search(r'[\\/:*?"<>|]', name) or name in (".", "..") or len(name) > 80:
            self.tg.send_text(chat, "⚠️ Bu isim klasör adı olamaz (\\ / : * ? \" < > | olmasın). Başka bir isim yaz:")
            return
        w["name"] = name
        w["await"] = None
        locs = self.wiz_locations()
        kb = [[{"text": n, "callback_data": "wz:l%d" % i}] for i, (n, _) in enumerate(locs)]
        kb.append([{"text": "✖️ İptal", "callback_data": "cancel"}])
        ex = next((p for n, p in locs if "Uygulamalarım" in n or "Projelerim" in n), None)
        hint = "\nÖrnek: %s\\%s\\main.py" % (ex.name, name) if ex else ""
        self.tg.send_text(chat, "📍 \"%s\" klasörü nereye oluşturulsun?%s" % (name, hint), kb)

    def wiz_step_msg(self, chat):
        w = self.wiz_ok()
        self.newproj = (WIZ_MARK, time.time())
        kind, val = w["proj"]
        pj = {"keep": "Şu anki proje: %s" % val, "none": "Projesiz (No Project)", "existing": "Proje: %s" % val,
              "new": "Yeni proje klasörü (Antigravity oluşturacak): %s" % val}[kind]
        self.tg.send_text(chat, "🆕 Yeni sohbet — 4/4  İlk mesaj\n\n🤖 Model: %s\n💻 Çalışma yeri: %s\n📁 %s\n\n"
                                "İlk mesajını yaz (dosya da gönderebilirsin)." % (
                              w.get("model") or "değişmeyecek", self.wiz_env_tr(w["env"]) if w.get("env") else "değişmeyecek", pj),
                          [[{"text": "✖️ İptal", "callback_data": "cancel"}]])

    def pick_menu_item(self, kind, want):
        """Bir açılır listeyi açar ve want yazılı öğeyi seçer."""
        b = self.cdp.agk("ctl(%s)" % json.dumps(kind))
        if not b:
            raise UIError("'%s' denetimi bulunamadı" % kind)
        self.cdp.click(b["x"], b["y"])
        time.sleep(0.9)
        pos = self.cdp.agk("menuPos(%s)" % json.dumps(want))
        if not pos:
            self.cdp.press_key("Escape", "Escape", 27)
            raise UIError("'%s' seçeneği listede görünmedi" % want)
        self.cdp.click(pos["x"], pos["y"])
        time.sleep(1.0)
        return pos

    def wiz_copy_path(self, path):
        """Klasör yolunu panoya koyar (kullanıcı pencerede yapıştırabilsin diye)."""
        if not IS_WIN:
            return False
        try:
            ps("Set-Clipboard -Value '%s'" % path.replace("'", "''"), 15)
            return True
        except Exception:
            return False

    def wiz_nav_new(self):
        if not self.cdp.agk("navNewClick()"):
            nav = self.cdp.agk("navNewPos()")
            if not nav:
                raise UIError("Soldaki 'New Conversation' düğmesini bulamadım")
            self.cdp.click(nav["x"], nav["y"])
        time.sleep(1.6)

    def apply_wizard(self):
        """Seçimleri yeni sohbet sayfasına uygular. Doğrulanamazsa mesaj yazılmaz."""
        w = self.wiz_ok()
        if not w:
            raise UIError("Sihirbazın süresi dolmuş")
        kind, val = w["proj"]
        newdir = None
        if kind == "new":
            newdir, kind = val, "none"      # klasörü Antigravity kendisi açar; sohbet projesiz başlar
        want = {"none": "No Project", "new": Path(val).name if kind == "new" else "", "existing": val, "keep": ""}[kind]

        def phase_a():
            self.wiz_nav_new()
            if kind == "none":
                self.pick_menu_item("proj", "No Project")
            elif kind == "existing":
                cur = (self.cdp.agk("ctl('proj')") or {}).get("text", "")
                if norm(val) != norm(cur):
                    self.pick_menu_item("proj", val)
            elif kind == "new":
                self.pick_menu_item("proj", "New Project")
        self.with_cdp(phase_a)

        def cur_proj():
            return self.with_cdp(lambda: (self.cdp.agk("ctl('proj')") or {}).get("text", ""))

        if False:
            self.wiz_copy_path(val)
            png = None
            try:
                png = self.with_cdp(self.cdp.screenshot)
            except Exception:
                pass
            self.say("📂 Antigravity'de \"Open workspace\" penceresi açıldı.\n\n"
                     "Windows güvenlik kuralları yüzünden bu pencereyi ben tıklayamıyorum. Senin yapacağın:\n"
                     "1) Pencerede şu klasörü seç: %s\n"
                     "   (Yolu panoya kopyaladım: pencerenin 'Klasör' kutusuna Ctrl+V yapıp Enter'a basabilirsin.)\n"
                     "2) 'Klasör Seç' düğmesine bas.\n\n"
                     "Seçince ben devam edip mesajını yazarım. 10 dakika bekliyorum." % val)
            if png:
                self.tg.send_photo(self.user_id, "pencere.png", png, "Açılan pencere")
        deadline = time.time() + (600 if kind == "new" else 12)
        now = ""
        while True:
            if kind == "keep":
                break
            now = cur_proj()
            if norm(want) in norm(now):
                break
            if time.time() > deadline:
                if kind == "new":
                    raise UIError("Klasör seçilmedi (10 dk doldu), mesaj yazılmadı. Açık pencere varsa İptal ile kapat.")
                raise UIError("Proje seçimini doğrulayamadım (şu an: %s). Hiçbir şey yazmadım." % (now or "?"))
            time.sleep(2 if kind == "new" else 0.7)

        def phase_c():
            if kind == "new":
                self.wiz_nav_new()   # yeni proje seçili kalarak boş sohbet sayfası
                now2 = (self.cdp.agk("ctl('proj')") or {}).get("text", "")
                if norm(want) not in norm(now2):
                    self.pick_menu_item("proj", want)
            if w.get("env"):
                self.pick_menu_item("env", w["env"])
            if w.get("model"):
                self.pick_menu_item("model", w["model"])
                nm = (self.cdp.agk("ctl('model')") or {}).get("text", "")
                if norm(w["model"]) not in norm(nm) and norm(nm) not in norm(w["model"]):
                    raise UIError("Model seçimini doğrulayamadım (şu an: %s). Hiçbir şey yazmadım." % (nm or "?"))
            if kind != "keep":
                fin = (self.cdp.agk("ctl('proj')") or {}).get("text", "")
                if norm(want) not in norm(fin):
                    raise UIError("Proje son kontrolde değişmiş (şu an: %s). Hiçbir şey yazmadım." % (fin or "?"))
        self.with_cdp(phase_c)
        self.wiz = None
        return newdir

    def do_retry(self, chat, cid):
        pc = self.convs.get(cid)
        if not pc:
            self.tg.send_text(chat, "Bu sohbeti bulamadım.")
            return
        with self.lock:
            self.open_conv(*pc)

            def f():
                pos = self.cdp.agk("retryBtn()")
                if not pos:
                    return False
                self.cdp.click(pos["x"], pos["y"])
                return True
            ok = self.with_cdp(f)
        self.tg.send_text(chat, ("🔁 Tekrar dene düğmesine basıldı: %s" % pc[1]) if ok else
                          "Bu sohbette 'Tekrar dene' düğmesi görünmüyor.")

    def ask_retry(self, chat, cid):
        pc = self.convs.get(cid)
        if not pc:
            self.tg.send_text(chat, "Bu sohbeti bulamadım.")
            return
        self.danger = (lambda: self.do_retry(chat, cid), time.time())
        self.tg.send_text(chat, "🔁 %s\nTekrar denenirse ajan aynı işlemleri (dosya değişikliği dahil) yeniden yapabilir. Devam edilsin mi?" % pc[1],
                          [[{"text": "✅ Evet, tekrar dene", "callback_data": "dok"}, {"text": "✖️ Vazgeç", "callback_data": "dno"}]])

    def cmd_sessiz(self, chat, arg):
        a = (arg or "").strip()
        if a.lower() in ("kapat", "kapali", "kapalı", "off"):
            save_env_value("SESSIZ", "")
            self.cfg["SESSIZ"] = ""
            self.tg.send_text(chat, "🔔 Sessiz saat kapatıldı.")
        elif re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", a):
            a = re.sub(r"\s+", "", a)
            save_env_value("SESSIZ", a)
            self.cfg["SESSIZ"] = a
            self.tg.send_text(chat, "🌙 Sessiz saat: %s\nBu saatlerde biten işler biriktirilir, sabah tek mesajla gelir. Onay bekleme, limit ve takılma uyarıları yine hemen gelir." % a)
        else:
            self.tg.send_text(chat, "Şu an: %s\nKullanım: /sessiz 23:00-07:00   ya da   /sessiz kapat" % (self.cfg.get("SESSIZ") or "kapalı"))

    # ------------------------------------------------------------------ GitHub yayın botu (githubbot)
    def gh_dir(self):
        d = self.cfg.get("GITHUBBOT_KLASOR")
        return Path(d) if d else BASE / "githubbot"

    def gh_run(self, args, timeout=1800):
        """yayinla.ps1 dosyasını çalıştırır; (çıkış kodu, temiz çıktı) döner."""
        script = self.gh_dir() / "yayinla.ps1"
        if not IS_WIN:
            return 1, "Bu özellik yalnızca Windows'ta çalışır."
        if not script.exists():
            return 1, "yayinla.ps1 bulunamadı: %s\n(ayarlar.env içine GITHUBBOT_KLASOR=... yazabilirsin)" % script
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)] + args,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               cwd=str(self.gh_dir()), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 1, "Zaman aşımı (%d dk). Yükleme tamamlanmamış olabilir; GitHub'da sürümü kontrol et." % (timeout // 60)
        out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + ("\n" + r.stderr if r.stderr else ""))
        return r.returncode, out.strip()

    def gh_pretty(self, out):
        lines = [l.rstrip() for l in out.splitlines() if l.strip()]
        res = []
        for l in lines:
            l = l.replace("[OK]", "✅").replace("[UYARI]", "⚠️").replace("DURDU:", "⛔ DURDU:")
            res.append(l)
        return "\n".join(res)

    def find_tv_apk(self):
        """TV APK'sını otomatik bulur: ayarlar.json 'tv_apk', masaüstü/indirilenler veya İzleProTv projesinin derleme klasörü."""
        home = Path.home()
        cands = []
        t = self.gh_cfg().get("tv_apk")
        if t:
            cands.append(Path(t))
        for d in (home / "Desktop" / "IzlePro_Web_Indirme_Sayfasi", home / "Desktop", home / "Downloads"):
            cands.append(d / "IzlePro_TV.apk")
        for b in (home / "Desktop" / "uygulamalarım" / "mobil", home / "Desktop" / "uygulamalar" / "mobil"):
            try:
                for x in b.iterdir():
                    if x.is_dir() and "tv" in x.name.lower():
                        cands.append(x / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk")
            except Exception:
                pass
        ok = [c for c in cands if c.is_file()]
        return max(ok, key=lambda c: c.stat().st_mtime) if ok else None

    def find_mobil_apk(self):
        """Mobil APK adayı: indirme sayfası klasörü / masaüstü / indirilenler ya da projenin derleme çıktısı (en yenisi)."""
        home = Path.home()
        cands = [home / "Desktop" / "IzlePro_Web_Indirme_Sayfasi" / "IzlePro_Mobil.apk", home / "Desktop" / "IzlePro_Mobil.apk",
                 home / "Downloads" / "IzlePro_Mobil.apk"]
        proj = self.gh_cfg().get("proje")
        if proj:
            cands.append(Path(proj) / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk")
        ok = [c for c in cands if c.is_file()]
        return max(ok, key=lambda c: c.stat().st_mtime) if ok else None

    def cmd_yayinla(self, chat, arg=""):
        a = (arg or "").strip().strip('"')
        if a.lower() in ("tvyok", "tv-yok", "tvsiz"):
            a, tvnote = "", "📺 TV APK: eklenmeyecek (sadece mobil)."
        elif a:
            tvnote = "📺 TV APK: %s" % a
        else:
            tvp = self.find_tv_apk()
            if tvp:
                a = str(tvp)
                st = tvp.stat()
                tvnote = "📺 TV APK otomatik bulundu:\n   %s\n   %s · %.1f MB" % (tvp, time.strftime("%d.%m.%Y %H:%M", time.localtime(st.st_mtime)), st.st_size / 1048576.0)
            else:
                tvnote = "📺 TV APK bulunamadı → sadece mobil yüklenir. (TV için dosyayı Masaüstüne IzlePro_TV.apk adıyla koy ya da /yayinla C:\\yol\\tv.apk yaz)"
        self.gh_tv = a if a else None
        self.gh_tvnote = tvnote
        mp = self.find_mobil_apk()
        self.gh_mobil = str(mp) if mp else None
        if mp:
            tvnote = "📱 Mobil APK: %s\n   %s · %.1f MB\n" % (mp, time.strftime("%d.%m.%Y %H:%M", time.localtime(mp.stat().st_mtime)), mp.stat().st_size / 1048576.0) + tvnote
        args = ["-Deneme"] + (["-TvApk", a] if a else []) + (["-MobilApk", self.gh_mobil] if self.gh_mobil else [])
        self.tg.send_text(chat, "🚀 GitHub yayın kontrolü başladı…\n(Hiçbir şey gönderilmez, sadece hazırlığı denetlerim.)")
        def work():
            code, out = self.gh_run(args, 300)
            txt = self.gh_pretty(out)
            if code == 0:
                self.gh_ready = time.time()
                self.tg.send_text(chat, "🚀 Yayın kontrolü\n" + LINE + "\n" + short(txt, 3200) +
                                  "\n\n" + tvnote + "\n\nHer şey hazır. Yayınlayayım mı?",
                                  [[{"text": ("⚠️ APK eski, yine de yükle" if "UYARI" in out else "✅ Evet, GitHub'a yükle"), "callback_data": "gh:go"}]]
                                  + ([[{"text": "📱 Sadece mobil yükle (TV'siz)", "callback_data": "gh:gonotv"}]] if self.gh_tv else [])
                                  + [[{"text": "✖️ Vazgeç", "callback_data": "gh:no"}]])
            else:
                hint = ""
                if "gh auth login" in out or "kurulu değil" in out:
                    hint = "\n\nBir kerelik kurulum gerekiyor (bilgisayarın başında): githubbot\\OKU.md dosyasındaki 1. ve 2. adım."
                self.tg.send_text(chat, "🚀 Yayın kontrolü\n" + LINE + "\n" + short(txt, 3200) + hint +
                                  "\n\n⛔ Yayınlanmadı. Sorunu giderince tekrar /yayinla yaz.")
        threading.Thread(target=work, daemon=True).start()

    def do_yayinla(self, chat):
        if time.time() - getattr(self, "gh_ready", 0) > 900:
            self.tg.send_text(chat, "⏱ Kontrolün süresi dolmuş (15 dk). Güvenlik için önce /yayinla ile yeniden kontrol et.")
            return
        self.gh_ready = 0
        if getattr(self, "gh_busy", False):
            self.tg.send_text(chat, "⏳ Zaten bir yükleme sürüyor, bitmesini bekle.")
            return
        self.gh_busy = True
        tv = getattr(self, "gh_tv", None)
        mo = getattr(self, "gh_mobil", None)
        args = ["-Evet"] + (["-TvApk", tv] if tv else []) + (["-MobilApk", mo] if mo else [])
        self.tg.send_text(chat, "⬆️ GitHub'a yükleniyor… APK büyükse birkaç dakika sürebilir. Bitince haber veririm.")
        def work():
            try:
                code, out = self.gh_run(args, 3600)
                txt = self.gh_pretty(out)
                log("GitHub yayın: kod %s" % code)
                if code == 0:
                    self.tg.send_text(chat, "🎉 Yayın tamam\n" + LINE + "\n" + short(txt, 3200))
                else:
                    self.tg.send_text(chat, "⚠️ Yayın tamamlanamadı\n" + LINE + "\n" + short(txt, 3200))
            finally:
                self.gh_busy = False
        threading.Thread(target=work, daemon=True).start()

    def cmd_surum(self, chat, arg=""):
        m = re.match(r"^\s*(\d+\.\d+\.\d+)\s+(\d+)\s*$", arg or "")
        if not m:
            self.tg.send_text(chat, "Kullanım: /surum 2.0.5 7\n(sürüm adı ve sürüm kodu). Proje dosyalarındaki sürümü günceller; derleme yapmaz.")
            return
        try:
            cfgp = self.gh_dir() / "ayarlar.json"
            proje = json.loads(cfgp.read_text(encoding="utf-8-sig"))["proje"]
        except Exception as e:
            self.tg.send_text(chat, "ayarlar.json okunamadı: %s" % e)
            return
        tool = Path(proje) / "araclar" / "surum_yap.py"
        if not tool.exists():
            self.tg.send_text(chat, "surum_yap.py bulunamadı: %s" % tool)
            return
        try:
            r = subprocess.run([sys.executable, str(tool), m.group(1), m.group(2)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120, cwd=str(proje))
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            self.tg.send_text(chat, ("✅ Sürüm güncellendi: %s (kod %s)\n" if r.returncode == 0 else "⚠️ Sürüm güncellenemedi\n") % (m.group(1), m.group(2)) +
                              short(out, 1500) + ("\n\nSıradaki adım: Android Studio'da APK'yı derle, sonra /yayinla." if r.returncode == 0 else ""))
        except Exception as e:
            self.tg.send_text(chat, "⚠️ Çalıştırılamadı: %s" % e)

    def cmd_guncelle(self, chat):
        repo = self.cfg.get("GITHUB_REPO", "").strip()
        base = os.environ.get("AGK_UPDATE_BASE") or ("https://raw.githubusercontent.com/%s/main" % repo if repo else "")
        if not base:
            self.tg.send_text(chat, "Güncelleme kaynağı ayarlı değil. ayarlar.env içine GITHUB_REPO=kullanici/depo yaz.\nŞu anki sürüm: %s" % VERSION)
            return
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/bridge.py", headers={"User-Agent": "agk"}), timeout=30) as r:
                code = r.read().decode("utf-8")
            m = re.search(r'^VERSION = "([^"]+)"', code, re.M)
            compile(code, "bridge.py", "exec")
        except Exception as e:
            self.tg.send_text(chat, "⚠️ Güncelleme kontrol edilemedi: %s" % e)
            return
        new = m.group(1) if m else "?"
        ver = lambda v: [int(x) for x in re.findall(r"\d+", v)]
        if not m or ver(new) <= ver(VERSION):
            self.tg.send_text(chat, "✅ Güncel (sürüm %s)." % VERSION)
            return
        self.update_pending = code
        self.tg.send_text(chat, "⬆️ Yeni sürüm var: %s (sende %s).\nİndirip köprüyü yeniden başlatayım mı? Eski dosya bridge.py.bak olarak saklanır; çalışan işler etkilenmez." % (new, VERSION),
                          [[{"text": "✅ Güncelle", "callback_data": "up"}, {"text": "✖️ Vazgeç", "callback_data": "dno"}]])

    def do_update(self, chat):
        code, self.update_pending = self.update_pending, None
        if not code:
            self.tg.send_text(chat, "Güncelleme isteği süresi dolmuş. /guncelle yaz.")
            return
        me = Path(__file__).resolve()
        shutil.copyfile(str(me), str(me) + ".bak")
        me.write_text(code, encoding="utf-8")
        self.tg.send_text(chat, "✅ Yeni sürüm yazıldı. Köprü yeniden başlıyor…")
        log("Güncellendi, yeniden başlatılıyor")
        if os.environ.get("AGK_NO_RESTART"):
            return
        vbs = BASE / "Kopru-Gizli-Baslat.vbs"
        if IS_WIN and vbs.exists():
            subprocess.Popen('cmd /c timeout /t 4 >nul & wscript "%s"' % vbs, shell=True,
                             creationflags=0x00000008 | 0x00000200)
        os._exit(0)

    def selected_cid(self):
        sel = self.selected_valid()
        if not sel:
            return None
        cid = hid(*sel)
        self.convs[cid] = sel
        return cid

    def handle_text(self, chat, text):
        t = text.strip()
        if self.pin_try(chat, t):
            return
        dpv = self.dp_valid()
        if dpv and dpv.get("await") == "name" and not t.startswith("/"):
            self.dp_name(chat, t)
            return
        w = self.wiz_ok()
        if w and w.get("await") == "name" and not t.startswith("/"):
            self.wiz_name(chat, t)
            return
        t = {BTN_PROJ: "/menu", BTN_DONE: "/biten", BTN_RUN: "/durum", BTN_SCHED: "/zamanlar", BTN_PC: "/pc",
                    BTN_NEW: "/yeni", BTN_TOOLS: "/araclar"}.get(t, t)
        m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", t, re.S)
        if not m:
            self.do_write(t)
            return
        cmd, arg = m.group(1).lower(), m.group(2)
        if cmd in ("start", "yardim", "help", "yardım"):
            self.tg.send_text(chat, HELP, KEYBOARD)
        elif cmd in ("menu", "liste", "projeler"):
            self.tg.send_text(chat, *self.projects_menu())
        elif cmd in ("biten", "bitenler"):
            self.tg.send_text(chat, *self.finished_menu())
        elif cmd == "durum":
            self.tg.send_text(chat, *self.running_menu())
        elif cmd in ("son", "cevap"):
            cid = self.selected_cid()
            if cid:
                self.send_answer(chat, cid)
            else:
                self.tg.send_text(chat, *self.projects_menu("Önce bir sohbet seç."))
        elif cmd == "dur":
            cid = self.selected_cid()
            self.tg.send_text(chat, self.do_stop(cid) if cid else "Önce bir sohbet seç. /menu")
        elif cmd == "dosya":
            self.cmd_dosya(chat, arg)
        elif cmd == "wa":
            a = (arg or "").strip().strip('"')
            if a:
                self.wa_pick(chat, a)
            else:
                self.tg.send_text(chat, "Kullanım: /wa C:\\yol\\uygulama.apk\nDosyayı WhatsApp'tan seçtiğin kişiye gönderir. İlk kurulum için /whatsapp")
        elif cmd == "whatsapp":
            self.cmd_whatsapp(chat)
        elif cmd == "wakisi":
            self.cmd_wakisi(chat, arg)
        elif cmd == "zamanla":
            self.cmd_zamanla(chat, arg)
        elif cmd in ("zamanlar", "zamanli"):
            self.tg.send_text(chat, *self.sched_menu())
        elif cmd in ("yayinla", "yayınla", "github"):
            if not self.pin_block(chat, "/yayinla"):
                self.cmd_yayinla(chat, arg)
        elif cmd == "surum":
            if not self.pin_block(chat, "/surum"):
                self.cmd_surum(chat, arg)
        elif cmd == "pin":
            self.cmd_pin(chat, arg)
        elif cmd in ("depoyukle", "depoyükle"):
            self.cmd_depoyukle(chat, arg)
        elif cmd in ("ozet", "özet"):
            self.cmd_ozet(chat, arg)
        elif cmd == "pc":
            self.cmd_pc(chat)
        elif cmd == "yeni":
            self.wiz_start(chat)
        elif cmd in ("araclar", "araçlar"):
            self.cmd_araclar(chat)
        elif cmd == "kilitle":
            if not self.pin_block(chat, "/kilitle"):
                self.cmd_kilitle(chat)
        elif cmd == "yenidenbaslat":
            if not self.pin_block(chat, "/yenidenbaslat"):
                self.cmd_yenidenbaslat(chat)
        elif cmd == "ekran":
            self.cmd_ekran(chat)
        elif cmd in ("saglik", "sağlık"):
            self.cmd_saglik(chat)
        elif cmd == "model":
            self.cmd_model(chat)
        elif cmd in ("tekrar", "retry"):
            cid = self.selected_cid()
            if cid:
                self.ask_retry(chat, cid)
            else:
                self.tg.send_text(chat, *self.projects_menu("Önce bir sohbet seç."))
        elif cmd == "sessiz":
            self.cmd_sessiz(chat, arg)
        elif cmd in ("guncelle", "güncelle"):
            if not self.pin_block(chat, "/guncelle"):
                self.cmd_guncelle(chat)
        elif cmd in ("teshis", "teşhis"):
            self.cmd_teshis(chat)
        elif cmd in ("bildirim", "bildirimler"):
            self.cmd_bildirim(chat, arg)
        elif cmd == "hesap":
            self.cmd_hesap(chat)
        elif cmd in ("depoyeni", "yenidepo"):
            self.cmd_depoyeni(chat, arg)
        elif cmd in ("kodgonder", "kodgönder"):
            if not self.pin_block(chat, "/kodgonder"):
                self.cmd_kodgonder(chat, arg)
        elif cmd in ("klasor", "klasör", "gozat"):
            self.cmd_klasor(chat, arg)
        elif cmd == "git":
            self.cmd_git(chat)
        elif cmd == "hata":
            self.cmd_hata(chat)
        elif cmd == "test":
            if not self.pin_block(chat, "/test"):
                self.cmd_test(chat)
        else:
            self.tg.send_text(chat, "Bilmediğim komut. /yardim yaz.")

    def handle_callback(self, cq):
        frm = (cq.get("from") or {}).get("id")
        qid = cq.get("id")
        if str(frm) != self.user_id:
            log("Yetkisiz düğme yoksayıldı (kullanıcı %s)" % frm)
            self.alert_unauth(frm, (cq.get("from") or {}).get("first_name"))
            return
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        data = cq.get("data", "")
        kind, _, arg = data.partition(":")
        slow = kind in ("ts", "dy", "kg", "dp", "dpd", "gha", "a", "l", "s", "send", "ap", "f", "fl", "wa", "wc", "m", "rt", "lz", "up", "dok", "vg", "rs") or data == "fsend"
        try:
            self.tg.call("answerCallbackQuery", {"callback_query_id": qid, "text": "⏳ Çalışıyorum…" if slow else ""})
        except Exception:
            pass

        def show(res):
            text, kb = res
            try:
                self.tg.edit_text(chat, mid, text, kb)
            except TgError:
                self.tg.send_text(chat, text, kb)

        gated = (kind in ("ts", "dy") or (kind == "dp" and arg in ("go", "link")) or (kind == "gh" and arg in ("go", "gonotv")) or (kind == "tl" and arg == "kilit_ok"))
        if gated and self.pin_block(chat, "düğme: " + kind):
            return
        try:
            if data == "home":
                show(self.projects_menu())
            elif data == "nn":
                self.wiz_start(chat)
            elif kind == "wz":
                self.wiz_callback(chat, arg)
            elif kind == "gh":
                if arg == "go":
                    self.do_yayinla(chat)
                elif arg == "gonotv":
                    self.gh_tv = None
                    self.do_yayinla(chat)
                else:
                    self.tg.send_text(chat, "Vazgeçildi. Hiçbir şey gönderilmedi.")
            elif kind == "mu":
                pc_ = self.convs.get(arg)
                if pc_:
                    self.muted.add(pc_[0] + "||" + pc_[1])
                    self.mute_save()
                    self.tg.send_text(chat, "🔕 Bu sohbet susturuldu:\n%s / %s\nGeri açmak için /bildirim." % (pc_[0], pc_[1]))
                else:
                    self.tg.send_text(chat, "Bu düğmenin süresi dolmuş.")
            elif kind == "bn":
                if arg in ("off", "on"):
                    save_env_value("BILDIRIM", "0" if arg == "off" else "1")
                    self.cfg["BILDIRIM"] = "0" if arg == "off" else "1"
                elif arg.startswith("s") and arg[1:].isdigit():
                    save_env_value("BILDIRIM_MIN_SN", arg[1:])
                    self.cfg["BILDIRIM_MIN_SN"] = arg[1:]
                elif arg == "unmute":
                    self.muted = set()
                    self.mute_save()
                show(self.notif_menu())
            elif kind == "gha":
                self.do_hesap(chat, arg)
            elif kind == "dpd":
                self.dp_start(chat, self.fmap.get(arg) or "")
            elif kind == "dp":
                dpv = self.dp_valid()
                if arg == "no":
                    self.dp = None
                    self.tg.send_text(chat, "Vazgeçildi. Hiçbir şey gönderilmedi.")
                elif not dpv:
                    self.tg.send_text(chat, "Süre doldu. /depoyeni ile yeniden başla.")
                elif arg == "name":
                    self.dp_name(chat, dpv.get("default", "yeni-proje"))
                elif arg == "priv":
                    dpv["vis"] = "private"
                    self.dp_scan_and_ask(chat)
                elif arg == "pub":
                    self.tg.send_text(chat, "🌍 Herkese açık depoyu İNTERNETTEKİ HERKES görür ve indirir. Gizli bir şey olmadığından eminsen onayla.",
                                      [[{"text": "🌍 Evet, herkese açık", "callback_data": "dp:pub2"}], [{"text": "🔒 Özel olsun", "callback_data": "dp:priv"}]])
                elif arg == "pub2":
                    dpv["vis"] = "public"
                    self.dp_scan_and_ask(chat)
                elif arg == "go":
                    self.dp_go(chat)
                elif arg == "link":
                    self.dp_link_go(chat)
                elif arg == "rename":
                    dpv["await"] = "name"
                    dpv["mode"] = "new"
                    dpv["default"] = dpv.get("name", "proje") + "-2"
                    dpv.pop("scan", None)
                    self.tg.send_text(chat, "Yeni depo adını yaz (öneri: %s)." % dpv["default"],
                                      [[{"text": "✅ " + dpv["default"], "callback_data": "dp:name"}], [{"text": "✖️ Vazgeç", "callback_data": "dp:no"}]])
            elif kind == "kb":
                show(self.browse_view(None if arg == "0" else self.fmap.get(arg)))
            elif kind == "kp":
                pid, _, pg = arg.partition(":")
                show(self.browse_view(self.fmap.get(pid), int(pg or 0)))
            elif kind == "kg":
                pth = self.fmap.get(arg)
                r = git_summary(pth) if pth else ""
                self.tg.send_text(chat, ("📁 %s\n" % pth + r) if r else "Bu klasör git deposu değil (ya da git kurulu değil).")
            elif kind == "kpin":
                self.pin_project_dir(chat, self.fmap.get(arg) or "")
            elif kind == "ts":
                self.do_test(chat)
            elif kind == "dy":
                self.do_depoyukle(chat)
            elif kind == "tl":
                if arg == "kilit_ok":
                    subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                    self.tg.send_text(chat, "🔒 Ekran kilitlendi.")
                elif arg in ("model", "tekrar", "dur", "ekran", "dosya", "whatsapp", "zamanlar", "sessiz", "saglik",
                             "pc", "kilitle", "guncelle", "teshis", "start", "yenidenbaslat", "yayinla",
                             "git", "hata", "test", "ozet", "klasor", "hesap", "depoyeni", "bildirim"):
                    self.handle_text(chat, "/" + arg)
            elif data == "pe":
                show(self.empties_menu())
            elif kind == "pg":
                show(self.projects_menu(page=int(arg or 0)))
            elif kind == "cg":
                pid, _, pg = arg.rpartition(":")
                show(self.convs_menu(pid, int(pg or 0)))
            elif kind == "p":
                show(self.convs_menu(arg))
            elif kind == "c":
                show(self.conv_panel(arg))
            elif kind == "n":
                project = self.projs.get(arg)
                if project is None:
                    show(self.projects_menu("Liste yenilendi."))
                else:
                    self.newproj = (project, time.time())
                    self.selected = None
                    self.tg.send_text(chat, "➕ Yeni sohbet\n📁 Proje: %s\n\nİlk mesajını yaz (dosya da gönderebilirsin). "
                                            "Mesajın bu projede yeni bir sohbet olarak başlar." % project,
                                      [[{"text": "✖️ İptal", "callback_data": "cancel"}]])
            elif kind == "a":
                self.send_answer(chat, arg)
            elif kind == "l":
                self.send_answer(chat, arg, tail=True)
            elif kind == "fl":
                self.send_files_of(chat, arg)
            elif kind == "f":
                path = self.fmap.get(arg)
                if path:
                    self.send_pc_file(chat, path)
                else:
                    self.tg.send_text(chat, "Bu düğmenin süresi dolmuş. Tekrar iste.")
            elif kind == "wa":
                path = self.fmap.get(arg)
                if path:
                    self.wa_pick(chat, path)
                else:
                    self.tg.send_text(chat, "Bu düğmenin süresi dolmuş. Tekrar iste.")
            elif kind == "wc":
                fid, _, i = arg.rpartition(":")
                path = self.fmap.get(fid)
                if path:
                    self.wa_send_async(chat, path, int(i))
                else:
                    self.tg.send_text(chat, "Bu düğmenin süresi dolmuş. Tekrar iste.")
            elif kind == "m":
                cid, _, i = arg.rpartition(":")
                self.do_model(chat, cid, int(i))
            elif kind == "md":
                self.select(self.convs[arg]) if arg in self.convs else None
                self.cmd_model(chat)
            elif kind == "rt":
                self.ask_retry(chat, arg)
            elif kind == "lz":
                cid, _, mins = arg.rpartition(":")
                pc = self.convs.get(cid)
                if pc:
                    ts = time.time() + int(mins) * 60
                    with self.slock:
                        self.sched.append({"id": uuid.uuid4().hex[:6], "ts": ts, "project": pc[0], "title": pc[1],
                                           "text": "Kaldığın yerden devam et.", "tries": 0})
                        self.sched_save()
                    self.tg.send_text(chat, "⏰ %s sonra (%s) \"devam et\" mesajı gönderilecek.\n💬 %s" % (
                        fmt_dur(int(mins) * 60), time.strftime("%H:%M", time.localtime(ts)), pc[1]))
                else:
                    self.tg.send_text(chat, "Bu sohbeti bulamadım.")
            elif data == "up":
                self.do_update(chat)
            elif kind == "s":
                self.tg.send_text(chat, self.do_stop(arg))
            elif kind == "ap":
                cid, _, i = arg.rpartition(":")
                self.do_approve(chat, cid, int(i))
            elif kind == "send":
                pc = self.convs.get(arg)
                if not pc or not (self.pending or self.files):
                    self.tg.send_text(chat, "Bekleyen mesaj yok.")
                else:
                    self.select(pc)
                    self.deliver(pc, self.pending)
            elif data == "fsend":
                sel = self.selected_valid()
                if not sel or not self.files:
                    self.tg.send_text(chat, "Gönderilecek dosya ya da seçili sohbet yok. 📁 Projeler'den seç.")
                else:
                    self.deliver(sel, "")
            elif kind == "cancel":
                self.pending = None
                self.files = []
                self.newproj = None
                self.wiz = None
                show(self.projects_menu("Bekleyen mesaj, dosyalar ve yeni sohbet isteği silindi."))
            elif data == "dok":
                d, self.danger = self.danger, None
                if d and time.time() - d[1] < DANGER_TTL:
                    d[0]()
                else:
                    self.tg.send_text(chat, "Bu onayın süresi dolmuş. Mesajı tekrar yaz.")
            elif data == "dno":
                self.danger = None
                self.tg.send_text(chat, "Vazgeçildi. Hiçbir şey gönderilmedi.")
            elif data == "vg":
                text, self.voice_pending = self.voice_pending, None
                if text:
                    self.do_write(text)
                else:
                    self.tg.send_text(chat, "Bu ses mesajının süresi dolmuş.")
            elif data == "vx":
                self.voice_pending = None
                self.tg.send_text(chat, "Vazgeçildi.")
            elif kind == "zd":
                with self.slock:
                    self.sched = [s for s in self.sched if s["id"] != arg]
                    self.sched_save()
                show(self.sched_menu())
            elif data == "rs":
                self.tg.send_text(chat, "🔄 Antigravity yeniden başlatılıyor…")
                self.restart_antigravity()
        except (CDPError, UIError) as e:
            self.tg.send_text(chat, "⚠️ Antigravity ile konuşamadım: %s" % e)
        except Exception as e:
            log("Düğme hatası:\n" + traceback.format_exc())
            self.tg.send_text(chat, "⚠️ Hata: %s" % e)

    def handle_update(self, u):
        if u.get("callback_query"):
            self.handle_callback(u["callback_query"])
            return
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            return
        frm = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        text = msg.get("text")
        if chat.get("type") != "private":
            return
        if not self.user_id:  # eşleşme modu
            if text and text.strip() == self.pair_code:
                self.user_id = str(frm)
                save_env_value("TELEGRAM_USER_ID", self.user_id)
                log("Eşleşti: kullanıcı %s" % self.user_id)
                state = {True: "Antigravity'ye bağlıyım ✅",
                         False: "Antigravity'ye henüz bağlanamadım (kısayoldan açtın mı?)"}.get(
                    self.cdp_ok, "Antigravity'ye bağlanıyorum…")
                self.tg.send_text(chat["id"], "✅ Eşleşme tamam. Artık yalnızca bu hesap beni kullanabilir.\n%s\n\n%s" % (state, HELP),
                                  KEYBOARD)
            elif text:
                self.tg.send_text(chat["id"], "Eşleşme bekleniyor. Bilgisayardaki 6 haneli kodu bana yaz.")
            return
        if str(frm) != self.user_id:
            log("Yetkisiz mesaj yoksayıldı (kullanıcı %s)" % frm)
            self.alert_unauth(frm, (msg.get("from") or {}).get("first_name"))
            return
        if msg.get("voice") and self.stt_ready():
            self.handle_voice(chat["id"], msg)
            return
        if msg.get("voice") and not self.stt_hint:
            self.stt_hint = True
            self.tg.send_text(chat["id"], "🎙 İpucu: sesli mesajları yazıya çevirebilirim. Bunun için klasördeki "
                                          "Ses-Kurulum.bat dosyasını bir kez çalıştır. Şimdilik sesi dosya olarak kaydediyorum.")
        media = extract_media(msg)
        if media:
            if self.save_incoming(chat["id"], media):
                with self.slock:
                    if msg.get("caption") and not self.cap:
                        self.cap = msg["caption"]
                self.schedule_flush(chat["id"])
            return
        if not text and msg.get("location"):
            loc = msg["location"]
            text = "Konumum: https://maps.google.com/?q=%s,%s" % (loc.get("latitude"), loc.get("longitude"))
        if not text and msg.get("contact"):
            c = msg["contact"]
            text = "Kişi: %s %s, telefon %s" % (c.get("first_name", ""), c.get("last_name", ""), c.get("phone_number", ""))
        if not text:
            self.tg.send_text(chat["id"], "Bu mesaj türünü alamıyorum. Yazı, fotoğraf, dosya, video, ses ve konum gönderebilirsin.")
            return
        self.cur_msg = (chat["id"], msg.get("message_id"))
        try:
            self.handle_text(chat["id"], text)
        except (CDPError, UIError) as e:
            self.tg.send_text(chat["id"], "⚠️ Antigravity ile konuşamadım: %s" % e)
        except Exception as e:
            log("Komut hatası:\n" + traceback.format_exc())
            self.tg.send_text(chat["id"], "⚠️ Hata: %s" % e)

    def telegram_loop(self):
        offset = None
        try:
            self.tg.call("setMyCommands", {"commands": COMMANDS})
        except Exception as e:
            log("Komut menüsü ayarlanamadı: %s" % e)
        while True:
            try:
                params = {"timeout": 25, "allowed_updates": ["message", "edited_message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                for u in self.tg.call("getUpdates", params, timeout=40):
                    offset = u["update_id"] + 1
                    try:
                        self.handle_update(u)
                    except Exception:
                        log("Güncelleme hatası:\n" + traceback.format_exc())
            except TgError as e:
                log("Telegram hatası: %s" % e)
                time.sleep(8)
            except (urllib.error.URLError, OSError, socket.timeout) as e:
                log("Telegram ağ hatası: %s" % e)
                time.sleep(6)

    def run(self):
        if not self.user_id and sys.stdout is None and IS_WIN:
            def popup():
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0, "Telegram'da botuna şu 6 haneli kodu yaz:\n\n        %s\n\n"
                           "(Bu kutuyu kapatabilirsin, kod geçerli kalır.)" % self.pair_code,
                        "Antigravity köprüsü - eşleşme kodu", 0x40 | 0x1000)
                except Exception:
                    pass
            threading.Thread(target=popup, daemon=True).start()
        if not self.user_id:
            print("\n" + "=" * 60)
            print("  EŞLEŞME KODU:  %s" % self.pair_code)
            print("  Telegram'da botuna bu 6 haneli kodu yaz.")
            print("  (Böylece bota sadece SEN komut verebilirsin.)")
            print("=" * 60 + "\n", flush=True)
        if self.cfg.get("KEEP_AWAKE", "1") != "0" and IS_WIN:
            self.keep_awake_on = True
            threading.Thread(target=keep_awake_loop, daemon=True).start()
        threading.Thread(target=self.telegram_loop, daemon=True).start()
        threading.Thread(target=self.scheduler_loop, daemon=True).start()
        threading.Thread(target=self.housekeeping_loop, daemon=True).start()
        threading.Thread(target=self.ozet_loop, daemon=True).start()
        self.monitor_loop()


# --------------------------------------------------------------------------- Windows yardımcıları
def ps(script, timeout=60):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def find_antigravity_exes(cfg):
    found = []
    if cfg.get("ANTIGRAVITY_EXE") and Path(cfg["ANTIGRAVITY_EXE"]).exists():
        found.append(cfg["ANTIGRAVITY_EXE"])
    if os.name == "nt":
        out = ps("Get-Process | Where-Object { $_.ProcessName -like '*ntigravity*' -and $_.MainWindowTitle } "
                 "| Select-Object -ExpandProperty Path -Unique")
        found += [l.strip() for l in out.splitlines() if l.strip().lower().endswith(".exe")]
        local = os.environ.get("LOCALAPPDATA", "")
        for p in (local + r"\Programs\Antigravity\Antigravity.exe",
                  local + r"\Programs\Antigravity IDE\Antigravity IDE.exe",
                  os.environ.get("ProgramFiles", "") + r"\Antigravity\Antigravity.exe"):
            if p and Path(p).exists():
                found.append(p)
        if not found:
            out = ps('$s=New-Object -ComObject WScript.Shell; '
                     'Get-ChildItem -Recurse -Filter "*ntigravity*.lnk" '
                     '"$env:APPDATA\\Microsoft\\Windows\\Start Menu","$env:ProgramData\\Microsoft\\Windows\\Start Menu" '
                     '-ErrorAction SilentlyContinue | ForEach-Object { $s.CreateShortcut($_.FullName).TargetPath }')
            found += [l.strip() for l in out.splitlines() if l.strip().lower().endswith(".exe")]
    seen, res = set(), []
    for p in found:
        if p.lower() not in seen and "uninstall" not in p.lower():
            seen.add(p.lower())
            res.append(p)
    return res


DEBUG_ARGS = ("--remote-debugging-port={port} --remote-allow-origins=* "
              "--disable-renderer-backgrounding --disable-backgrounding-occluded-windows "
              "--disable-background-timer-throttling")


def cmd_kisayol(cfg):
    exes = find_antigravity_exes(cfg)
    if not exes:
        print("Antigravity'nin kurulu olduğu yeri bulamadım.")
        print("Antigravity açıkken bu adımı tekrar dene ya da ayarlar.env içine ANTIGRAVITY_EXE=... yaz.")
        return []
    args = DEBUG_ARGS.format(port=cfg["CDP_PORT"])
    made = []
    for exe in exes:
        name = "Antigravity (Telegram)" if len(exes) == 1 else "Antigravity (Telegram) - %s" % Path(exe).stem
        script = (
            "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut("
            "(Join-Path $d '%s.lnk')); $s.TargetPath='%s'; $s.Arguments='%s'; $s.WorkingDirectory='%s'; "
            "$s.IconLocation='%s,0'; $s.Save()" % (name, exe, args, str(Path(exe).parent), exe))
        ps(script)
        made.append((name, exe))
        print("Masaüstüne kısayol konuldu: %s" % name)
    return made


def cmd_otomatik():
    if os.name != "nt":
        print("Otomatik başlatma yalnızca Windows'ta kurulur.")
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py = str(pyw if pyw.exists() else sys.executable)
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    startup.mkdir(parents=True, exist_ok=True)
    vbs = startup / "AntigravityTelegramKopru.vbs"
    vbs.write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.CurrentDirectory = "%s"\r\n'
        'sh.Run """%s"" ""%s""", 0, False\r\n' % (str(BASE), py, str(BASE / "bridge.py")),
        encoding="mbcs", errors="replace")
    print("Windows açılışında köprü gizli olarak kendiliğinden başlayacak.")


def cmd_baslat(cfg):
    """Antigravity'yi debug portuyla açar."""
    cdp = CDP(cfg["CDP_PORT"], cfg["UI_MARKERS"])
    if cdp.available():
        print("Antigravity zaten köprüye hazır durumda (port %s açık)." % cfg["CDP_PORT"])
        return
    exes = find_antigravity_exes(cfg)
    if not exes:
        print("Antigravity'yi bulamadım. Önce normal şekilde açıp bu dosyayı tekrar çalıştır.")
        return
    running = ps("Get-Process | Where-Object { $_.ProcessName -like '*ntigravity*' -and $_.MainWindowTitle } "
                 "| Select-Object -ExpandProperty Id") if os.name == "nt" else ""
    if running:
        print("Antigravity şu an AÇIK ama köprü portu olmadan çalışıyor.")
        print("Kapatıp yeniden açmam gerekiyor. DİKKAT: Çalışan işler yarıda kalır!")
        if input("Şimdi kapatıp köprülü açayım mı? (E/H): ").strip().lower() not in ("e", "evet", "y"):
            print("Tamam, dokunmadım. İşler bitince tekrar çalıştır.")
            return
        ps("Get-Process | Where-Object { $_.ProcessName -like '*ntigravity*' } | Stop-Process -Force")
        time.sleep(3)
    subprocess.Popen([exes[0]] + DEBUG_ARGS.format(port=cfg["CDP_PORT"]).split(" "))
    print("Antigravity köprülü olarak açılıyor: %s" % exes[0])


# --------------------------------------------------------------------------- kurulum / test / giriş
# --------------------------------------------------------------------------- Antigravity yönetimi
def antigravity_running():
    if os.name != "nt":
        return False
    out = ps("Get-Process | Where-Object { $_.ProcessName -like '*ntigravity*' } | Select-Object -ExpandProperty Id", 20)
    return bool(out.strip())


def kill_antigravity():
    if os.name == "nt":
        ps("Get-Process | Where-Object { $_.ProcessName -like '*ntigravity*' } | Stop-Process -Force", 30)


def pick_exe(exes, cfg):
    return exes[0] if exes else None


def launch_antigravity(exe, cfg):
    subprocess.Popen([exe] + DEBUG_ARGS.format(port=cfg["CDP_PORT"]).split(" "))


def detect_port_exe(port):
    """Köprü portuyla çalışan Antigravity'nin exe yolunu bulur."""
    if os.name != "nt":
        return None
    out = ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*remote-debugging-port=%s*' -and $_.Name -like '*ntigravity*' } "
             "| Select-Object -First 1 -ExpandProperty ExecutablePath" % port, 20)
    return out.strip() or None


def mask_token(t):
    t = t or ""
    return (t[:6] + "…" + t[-4:]) if len(t) > 14 else ("(yok)" if not t else "***")


def ask_token(cfg):
    """Kullanıcıdan bot anahtarını ister, Telegram'a sorup doğrular ve kaydeder."""
    print("Telegram'da @BotFather'a /newbot yazıp aldığın bot anahtarını (token) yapıştır.")
    print("(Yapıştırmak için pencereye sağ tıkla, Enter'a bas.)")
    tok = input("Bot anahtarı: ").strip()
    if not tok:
        print("Anahtar girilmedi, değişiklik yapılmadı.")
        return False
    try:
        me = Telegram(tok, cfg["TELEGRAM_API_BASE"]).call("getMe")
    except Exception as e:
        print("Bu anahtar çalışmadı: %s\nKopyalarken eksik/fazla karakter kalmış olabilir. Tekrar dene." % e)
        return False
    save_env_value("TELEGRAM_BOT_TOKEN", tok)
    cfg["TELEGRAM_BOT_TOKEN"] = tok
    print("Bot doğrulandı: @%s" % me.get("username"))
    return True


PID_FILE = BASE / "kopru.pid"


def stop_bridge():
    """Bu klasörden başlatılmış köprüyü durdurur (başka klasördeki köprüye dokunmaz)."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return
    if pid == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=20)
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    time.sleep(1.5)


def remove_path(p):
    try:
        if p.is_dir():
            shutil.rmtree(str(p), ignore_errors=True)
        elif p.exists():
            p.unlink()
        return not p.exists()
    except OSError:
        return False


def cmd_temizle():
    """Kişisel her şeyi siler: token, eşleşme, günlükler, gelen dosyalar, zamanlamalar, otomatik başlatma."""
    print("\n=== KİŞİSEL VERİLERİ SİL (paylaşmadan önce) ===")
    print("Silinecekler: bot anahtarı (token), Telegram eşleşmesi, ayarlar.env, günlükler, teşhis dosyaları,")
    print("zamanlanmış mesajlar, 'gelen' klasörü, önbellek ve Windows açılışındaki otomatik başlatma.")
    if input("Emin misin? Devam için EVET yaz: ").strip().upper() != "EVET":
        print("Vazgeçildi, hiçbir şey silinmedi.")
        return
    stop_bridge()
    targets = [ENV_FILE, LOG_FILE, PID_FILE, BASE / "teshis.json", BASE / "teshis_oto.json", BASE / "whatsapp-profil", BASE / "zamanlanmis.json",
               BASE / "sched_test.json", BASE / "gelen", BASE / "__pycache__"]
    if os.name == "nt":
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup" / "AntigravityTelegramKopru.vbs"
        try:
            if startup.exists() and str(BASE).lower() in startup.read_text(encoding="mbcs", errors="replace").lower():
                targets.append(startup)  # yalnızca bu klasörü başlatan kayıt silinir
        except OSError:
            pass
    for t in targets:
        if t.exists():
            print("  %s  %s" % ("silindi " if remove_path(t) else "SİLİNEMEDİ", t.name))
    print("\nTamam. Bu klasörde artık sana ait token/eşleşme yok; GitHub'a yükleyebilirsin.")
    print("ÖNEMLİ: Eski anahtarın Telegram'da hâlâ geçerli olabilir. Tamamen geçersiz yapmak için")
    print("Telegram'da @BotFather -> /revoke yaz ve botunu seç (ya da botu /deletebot ile sil).")


def cmd_ayarlar(cfg):
    while True:
        print("\n=== AYARLAR ===")
        print("Bot anahtarı : %s" % mask_token(cfg["TELEGRAM_BOT_TOKEN"]))
        print("Eşleşen kişi : %s" % (cfg["TELEGRAM_USER_ID"] or "yok (eşleşme bekliyor)"))
        print("\n 1) Bot anahtarını (token) gir / değiştir")
        print(" 2) Eşleşmeyi sıfırla (başka Telegram hesabıyla eşleşmek için)")
        print(" 3) Kişisel verileri sil (GitHub'da paylaşmadan önce)")
        print(" 4) Çık")
        c = input("Seçim (1-4): ").strip()
        if c == "1":
            if ask_token(cfg):
                save_env_value("TELEGRAM_USER_ID", "")
                cfg["TELEGRAM_USER_ID"] = ""
                print("Yeni bot için eşleşme sıfırlandı. Köprüyü yeniden başlatınca yeni kod gösterilecek.")
        elif c == "2":
            save_env_value("TELEGRAM_USER_ID", "")
            cfg["TELEGRAM_USER_ID"] = ""
            print("Eşleşme silindi. Köprüyü yeniden başlatınca yeni 6 haneli kod gösterilecek.")
        elif c == "3":
            cmd_temizle()
            return
        elif c == "4" or not c:
            return


def cmd_kurulum(cfg):
    print("\n=== Antigravity <-> Telegram köprüsü kurulumu ===\n")
    if cfg["TELEGRAM_BOT_TOKEN"]:
        print("Kayıtlı bot anahtarı var: %s" % mask_token(cfg["TELEGRAM_BOT_TOKEN"]))
        if input("Bu botu kullanmaya devam edeyim mi? (E/H): ").strip().lower() in ("h", "hayır", "hayir", "n"):
            if not ask_token(cfg):
                return None
            save_env_value("TELEGRAM_USER_ID", "")
            cfg["TELEGRAM_USER_ID"] = ""
    else:
        if not ask_token(cfg):
            return None
    cmd_kisayol(cfg)
    cmd_otomatik()
    print("\nKurulum tamam. Şimdi köprü açılacak ve eşleşme kodunu gösterecek.\n")
    return cfg


def cmd_test(cfg):
    ok = True
    try:
        me = Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_API_BASE"]).call("getMe")
        print("Telegram: tamam (@%s)" % me.get("username"))
    except Exception as e:
        ok = False
        print("Telegram: SORUN -> %s" % e)
    cdp = CDP(cfg["CDP_PORT"], cfg["UI_MARKERS"])
    try:
        cdp.connect()
        rows = cdp.agk("rows()") or []
        convs = [r for r in rows if r["kind"] == "conv"]
        print("Antigravity: tamam (%d konuşma görüyorum, %d çalışıyor)" % (len(convs), sum(1 for r in convs if r["running"])))
    except Exception as e:
        ok = False
        print("Antigravity: SORUN -> %s" % e)
    print("Kullanıcı eşleşmesi: %s" % ("var" if cfg["TELEGRAM_USER_ID"] else "henüz yok"))
    return ok


def single_instance():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return None
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kurulum", action="store_true")
    ap.add_argument("--baslat", action="store_true", help="Antigravity'yi köprü portuyla aç")
    ap.add_argument("--kisayol", action="store_true")
    ap.add_argument("--otomatik", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--ayarlar", action="store_true", help="Ayarlar menüsü (token, eşleşme, temizlik)")
    ap.add_argument("--temizle", action="store_true", help="Kişisel verileri sil")
    args = ap.parse_args()
    cfg = load_env()

    if args.ayarlar:
        return cmd_ayarlar(cfg)
    if args.temizle:
        return cmd_temizle()
    if args.baslat:
        return cmd_baslat(cfg)
    if args.kisayol:
        cmd_kisayol(cfg)
        return
    if args.otomatik:
        return cmd_otomatik()
    if args.test:
        sys.exit(0 if cmd_test(cfg) else 1)
    if args.kurulum:
        cfg = cmd_kurulum(cfg)
        if not cfg:
            return
        if os.name == "nt" and (BASE / "Kopru-Gizli-Baslat.vbs").exists():
            subprocess.Popen(["wscript", str(BASE / "Kopru-Gizli-Baslat.vbs")])
            print("Köprü arka planda (gizli) başlatıldı. Ekranda eşleşme kodu kutusu çıkacak.")
            print("Bu pencereyi kapatabilirsin.")
            return

    if not cfg["TELEGRAM_BOT_TOKEN"]:
        print("Bot anahtarı yok. Önce Kurulum.bat dosyasını çalıştır.")
        return
    lock = single_instance()
    if lock is None:
        print("Köprü zaten çalışıyor.")
        return
    log("Köprü başlıyor")
    try:
        PID_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    Bridge(cfg).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
