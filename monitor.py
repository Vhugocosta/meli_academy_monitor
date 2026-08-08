#!/usr/bin/env python3
"""
Monitor Mercado Ads Academy for new certifications.
Sends Telegram + Email alerts when target certifications appear.
Supports multiple Telegram chat IDs and multiple email recipients
(comma-separated in the respective env vars).
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
# Credentials from environment (set as GitHub Secrets)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")   # can be "id1,id2,..."

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)          # can be "a@x,b@y,..."

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
# With GitHub Actions running every 5 min, 6 alerts = ~30 minutes of nagging.
# Increase to 12 for ~1h of alerts, 24 for ~2h.
MAX_ALERTS_PER_CERT = 20

# Timezone for all timestamps in logs and messages
TZ = ZoneInfo("America/Sao_Paulo")

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"

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
    # In GitHub Actions, prints show up directly in the workflow log
    print(f"[{now().strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}")


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

    chat_ids = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]
    all_ok = True

    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            log(f"Telegram send failed for chat_id={chat_id}: {e}")
            all_ok = False

    return all_ok


def send_email(subject: str, body: str) -> bool:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log("Gmail not configured, skipping.")
        return False

    # Support comma-separated list of recipients
    recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
    if not recipients:
        log("No email recipients configured, skipping.")
        return False

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        # Header "To" is a human-readable string joining all recipients
        msg["To"] = ", ".join(recipients)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            # SMTP recipients argument must be a LIST — this is the actual
            # delivery envelope. The Gmail API rejects strings with commas.
            smtp.sendmail(GMAIL_USER, recipients, msg.as_string())
        log(f"Email sent to {len(recipients)} recipient(s): {recipients}")
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
            if entry.get("alert_count", 0) < MAX_ALERTS_PER_CERT:
                next_num = entry.get("alert_count", 0) + 1
                alert(cert, url, snippet,
                      alert_num=next_num, total=MAX_ALERTS_PER_CERT)
                entry["last_alert_at"] = now().isoformat()
                entry["alert_count"] = next_num
                entry["last_snippet"] = snippet

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
