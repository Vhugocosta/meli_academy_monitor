#!/usr/bin/env python3
"""
Monitor Mercado Ads Academy for new certifications.
Sends Telegram + Email alerts when target certifications appear.
Repeats alert every run for up to MAX_ALERTS_PER_CERT after first detection.
Dedupes hits across multiple monitored URLs (1 alert per cert per run).
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ============ CONFIG ============
# Credentials from environment (see README)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)

# Target certifications (case-insensitive substring match)
TARGETS = [
    "Retail Media Search Expert",
    "Retail Media Insights Strategist",
]

# Pages to monitor
URLS = [
    "https://academy.mercadoads.com/student/catalog?locale=pt-BR",
    "https://academy.mercadoads.com/student/catalog?locale=es-419",
    "https://academy.mercadoads.com/student/catalog",
]

# How many alerts to fire per certification.
# With cron running every 1 min, this equals ~MAX_ALERTS_PER_CERT minutes
# of continuous nagging after first detection.
MAX_ALERTS_PER_CERT = 30

# Timezone for all timestamps in logs and messages
TZ = ZoneInfo("America/Sao_Paulo")

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "monitor.log"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8,es;q=0.7",
}

TIMEOUT = 30
# ================================


def now() -> datetime:
    return datetime.now(TZ)


def fmt(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"WARN: could not parse state file, resetting: {e}")
    return {"found": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def find_hits(text: str) -> list[tuple[str, str]]:
    """Return list of (target_name, ~120 chars of context around match)."""
    hits = []
    lower = text.lower()
    for target in TARGETS:
        idx = lower.find(target.lower())
        if idx >= 0:
            start = max(0, idx - 60)
            end = min(len(text), idx + len(target) + 60)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            hits.append((target, snippet))
    return hits


def send_telegram(msg: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log("Telegram not configured, skipping.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def send_email(subject: str, body: str) -> bool:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log("Gmail not configured, skipping.")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, [EMAIL_TO], msg.as_string())
        return True
    except Exception as e:
        log(f"Email send failed: {e}")
        return False


def alert(cert_name: str, url_found: str, snippet: str,
          alert_num: int, total: int) -> None:
    when = fmt(now())
    header = "🚨 NOVA CERTIFICAÇÃO DISPONÍVEL!" if alert_num == 1 \
        else f"🔔 Lembrete {alert_num}/{total}"

    telegram_msg = (
        f"<b>{header}</b>\n\n"
        f"📚 <b>{cert_name}</b>\n\n"
        f"🔗 <a href=\"{url_found}\">Abrir Mercado Ads Academy</a>\n\n"
        f"⏰ {when} (São Paulo)\n"
        f"📢 Alerta {alert_num} de {total}\n\n"
        f"<i>Contexto da página:</i>\n"
        f"<code>...{snippet}...</code>"
    )
    if alert_num == 1:
        telegram_msg += "\n\nCorre! 🏃💨"

    email_body = (
        f"{header}\n\n"
        f"Certificação: {cert_name}\n"
        f"Detectada/relembrada em: {when} (São Paulo)\n"
        f"Alerta {alert_num} de {total}\n"
        f"Página onde apareceu: {url_found}\n\n"
        f"Contexto na página:\n...{snippet}...\n\n"
        f"Acesse agora: https://academy.mercadoads.com/student/catalog?locale=pt-BR\n"
    )

    subject_prefix = "🚨" if alert_num == 1 else f"🔔 [{alert_num}/{total}]"
    subject = f"{subject_prefix} {cert_name}"

    tg_ok = send_telegram(telegram_msg)
    em_ok = send_email(subject, email_body)
    log(f"Alert {alert_num}/{total} — cert='{cert_name}' telegram={tg_ok} email={em_ok}")


def main() -> int:
    state = load_state()
    found_map = state.setdefault("found", {})

    # 1) Collect hits across all URLs, deduped by certification name.
    #    If the same cert appears on multiple locales, we keep only the
    #    first URL where it was found — one alert per cert per run.
    hits_by_cert: dict[str, tuple[str, str]] = {}
    for url in URLS:
        try:
            html = fetch_html(url)
        except Exception as e:
            log(f"Fetch failed for {url}: {e}")
            continue

        for cert, snippet in find_hits(html):
            hits_by_cert.setdefault(cert, (url, snippet))

    # 2) Process each unique cert once.
    for cert, (url, snippet) in hits_by_cert.items():
        entry = found_map.get(cert)

        if entry is None:
            # First ever detection: alert 1/N
            alert(cert, url, snippet, alert_num=1, total=MAX_ALERTS_PER_CERT)
            found_map[cert] = {
                "url": url,
                "detected_at": now().isoformat(),
                "last_alert_at": now().isoformat(),
                "alert_count": 1,
                "last_snippet": snippet,
            }
            log(f"MATCH (new): '{cert}' at {url}")
        else:
            # Already known: keep alerting until MAX_ALERTS_PER_CERT
            if entry.get("alert_count", 0) < MAX_ALERTS_PER_CERT:
                next_num = entry.get("alert_count", 0) + 1
                alert(cert, url, snippet,
                      alert_num=next_num, total=MAX_ALERTS_PER_CERT)
                entry["last_alert_at"] = now().isoformat()
                entry["alert_count"] = next_num
                entry["last_snippet"] = snippet
            # else: quota reached, stay silent

    save_state(state)

    # 3) Summary
    still_waiting = [t for t in TARGETS if t not in found_map]
    still_nagging = [
        f"{k} ({v['alert_count']}/{MAX_ALERTS_PER_CERT})"
        for k, v in found_map.items()
        if v.get("alert_count", 0) < MAX_ALERTS_PER_CERT and k in TARGETS
    ]
    done = [
        k for k, v in found_map.items()
        if v.get("alert_count", 0) >= MAX_ALERTS_PER_CERT and k in TARGETS
    ]

    log(
        f"Run finished. "
        f"waiting={still_waiting or 'none'} "
        f"nagging={still_nagging or 'none'} "
        f"done={done or 'none'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())