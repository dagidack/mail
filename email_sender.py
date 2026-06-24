"""Core email sending logic: CSV parsing, SMTP delivery, randomized delays."""

import csv
import io
import os
import random
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

from dotenv import load_dotenv

load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "").replace(" ", "")
SENDER_NAME = os.getenv("SENDER_NAME", "Renas Media")

SUBJECT_TEMPLATE = os.getenv(
    "SUBJECT_TEMPLATE", "Video content for {Company_Name}"
)

HTML_TEMPLATE = """\
<html>
  <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.5; font-size: 14px;">
    <p>Hi {Name},</p>
    <p>I noticed the great work <b>{Company_Name}</b> is doing, and I wanted to reach out.</p>
    <p>I'm with Renas Media, a digital production team based in Finland.</p>
    <br>
    <p>Best regards,<br><b>Maria V.</b></p>
  </body>
</html>
"""

VIMEO_URL = os.getenv("VIMEO_URL", "https://vimeo.com/renasmedia")
DELAY_MIN = float(os.getenv("DELAY_MIN_SECONDS", "10"))
DELAY_MAX = float(os.getenv("DELAY_MAX_SECONDS", "20"))

_COLUMN_ALIASES = {
    "email": "email", "e-mail": "email", "mail": "email",
    "name": "Name", "first name": "Name", "firstname": "Name",
    "company": "Company_Name", "company_name": "Company_Name",
    "company name": "Company_Name", "organization": "Company_Name",
}

def _normalize_column(header: str) -> Optional[str]:
    key = header.strip().lower()
    if key in _COLUMN_ALIASES:
        return _COLUMN_ALIASES[key]
    stripped = header.strip()
    if stripped in ("Name", "Company_Name"):
        return stripped
    return None

def parse_csv(content: str | bytes) -> tuple[list[dict], list[str]]:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return [], ["CSV has no header row."]

    column_map: dict[str, str] = {}
    for header in reader.fieldnames:
        canonical = _normalize_column(header)
        if canonical:
            column_map[header] = canonical

    if "email" not in column_map.values():
        return [], ["CSV must include an 'email' column."]

    receivers: list[dict] = []
    errors: list[str] = []

    for row_num, row in enumerate(reader, start=2):
        receiver = {"_original_row": dict(row)} # Сохраняем все оригинальные данные
        for header, canonical in column_map.items():
            value = (row.get(header) or "").strip()
            if value:
                receiver[canonical] = value

        email = receiver.get("email", "")
        if not email:
            errors.append(f"Row {row_num}: missing email — skipped.")
            continue

        if "@" not in email:
            errors.append(f"Row {row_num}: invalid email '{email}' — skipped.")
            continue

        receiver.setdefault("Name", email.split("@")[0])
        receiver.setdefault("Company_Name", "your company")
        receivers.append(receiver)

    return receivers, errors

def validate_config() -> list[str]:
    problems = []
    if not SENDER_EMAIL:
        problems.append("SENDER_EMAIL is not set (check .env).")
    if not SENDER_PASSWORD:
        problems.append("SENDER_PASSWORD is not set (check .env).")
    return problems

def random_delay() -> float:
    return random.uniform(DELAY_MIN, DELAY_MAX)

def get_default_templates() -> dict[str, str]:
    return {"subject_template": SUBJECT_TEMPLATE, "html_template": HTML_TEMPLATE}

def apply_placeholders(template: str, receiver: dict, vimeo_url: str = VIMEO_URL) -> str:
    return (
        template.replace("{Name}", receiver.get("Name", ""))
        .replace("{Company_Name}", receiver.get("Company_Name", ""))
        .replace("{Vimeo_URL}", vimeo_url)
    )

def send_email(receiver: dict, server: smtplib.SMTP, subject_template: str, html_template: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = receiver["email"]
    msg["Subject"] = apply_placeholders(subject_template, receiver)
    body = apply_placeholders(html_template, receiver)
    msg.attach(MIMEText(body, "html"))
    server.send_message(msg)

def save_results_csv(receivers: list[dict], summary: dict, output_filename: str) -> None:
    """Генерирует итоговый файл с отметками sent/failed"""
    if not receivers:
        return
        
    fieldnames = list(receivers[0].get("_original_row", {}).keys())
    if not fieldnames:
        return
        
    if "Status" not in fieldnames:
        fieldnames.append("Status")
    if "Error" not in fieldnames:
        fieldnames.append("Error")

    with open(output_filename, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, rec in enumerate(receivers):
            row_out = dict(rec.get("_original_row", {}))
            if i < len(summary["results"]):
                res = summary["results"][i]
                row_out["Status"] = res.get("status", "skipped")
                row_out["Error"] = res.get("error", "")
            else:
                row_out["Status"] = "skipped"
                row_out["Error"] = ""
            writer.writerow(row_out)

StatusCallback = Callable[[dict], None]

def run_campaign(
    receivers: list[dict],
    on_status: Optional[StatusCallback] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    subject_template: Optional[str] = None,
    html_template: Optional[str] = None,
    output_filename: Optional[str] = None, # Новый параметр для файла
) -> dict:
    
    summary = {"total": len(receivers), "sent": 0, "failed": 0, "results": [], "stopped": False}

    def emit(event: dict) -> None:
        if on_status: on_status(event)

    subject_tpl = subject_template or SUBJECT_TEMPLATE
    html_tpl = html_template or HTML_TEMPLATE

    config_errors = validate_config()
    if config_errors:
        emit({"type": "error", "message": "; ".join(config_errors)})
        return summary

    emit({"type": "connecting", "message": "Connecting to SMTP server…"})

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
    except Exception as exc:
        emit({"type": "error", "message": f"Connection failed: {exc}"})
        return summary

    emit({"type": "started", "message": "Authorized. Starting send…", "total": len(receivers)})

    for index, person in enumerate(receivers):
        if should_stop and should_stop():
            summary["stopped"] = True
            emit({"type": "stopped", "message": "Send stopped by user."})
            break

        email = person["email"]
        emit({"type": "sending", "index": index + 1, "total": len(receivers), "email": email})

        try:
            send_email(person, server, subject_tpl, html_tpl)
            summary["sent"] += 1
            summary["results"].append({"email": email, "status": "sent"})
            emit({"type": "sent", "index": index + 1, "total": len(receivers), "email": email})
        except Exception as exc:
            summary["failed"] += 1
            summary["results"].append({"email": email, "status": "failed", "error": str(exc)})
            emit({"type": "failed", "index": index + 1, "total": len(receivers), "email": email, "error": str(exc)})

        if index < len(receivers) - 1 and not (should_stop and should_stop()):
            delay = random_delay()
            emit({"type": "waiting", "seconds": round(delay, 1), "next_index": index + 2, "total": len(receivers)})
            elapsed = 0.0
            while elapsed < delay:
                if should_stop and should_stop():
                    summary["stopped"] = True
                    emit({"type": "stopped", "message": "Send stopped by user."})
                    break
                time.sleep(min(0.5, delay - elapsed))
                elapsed += 0.5
            if summary["stopped"]:
                break

    # === СОХРАНЯЕМ ИТОГОВЫЙ CSV ФАЙЛ ===
    if output_filename:
        try:
            save_results_csv(receivers, summary, output_filename)
            msg_suffix = f" (Results saved to: {output_filename})"
        except Exception as e:
            msg_suffix = f" (Failed to save CSV: {e})"
    else:
        msg_suffix = ""

    try:
        server.quit()
    except Exception:
        pass

    if not summary["stopped"]:
        emit({"type": "done", "message": f"Campaign complete.{msg_suffix}", "sent": summary["sent"], "failed": summary["failed"]})
    else:
        if output_filename:
            emit({"type": "info", "message": f"Partial results saved to: {output_filename}"})

    return summary