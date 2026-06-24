"""Web UI for CSV-based email campaigns — runs on your local network."""

import threading
import uuid
from collections import deque

from flask import Flask, jsonify, render_template, request

from email_sender import (
    DELAY_MAX,
    DELAY_MIN,
    HTML_TEMPLATE,
    SENDER_EMAIL,
    SUBJECT_TEMPLATE,
    get_default_templates,
    parse_csv,
    run_campaign,
    validate_config,
)

app = Flask(__name__)

# --- in-memory job state (fine for single-user local use) ---
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_active_job_id: str | None = None
_stop_flags: dict[str, bool] = {}


def _create_job(receivers: list[dict], parse_errors: list[str], filename: str) -> str:
    job_id = str(uuid.uuid4())
    with _lock:
        defaults = get_default_templates()
        _jobs[job_id] = {
            "id": job_id,
            "filename": filename,
            "status": "pending",
            "receivers": receivers,
            "parse_errors": parse_errors,
            "events": deque(maxlen=500),
            "summary": None,
            "subject_template": defaults["subject_template"],
            "html_template": defaults["html_template"],
        }
        _stop_flags[job_id] = False
    return job_id


def _append_event(job_id: str, event: dict) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["events"].append(event)
            if event.get("type") in ("started", "sending", "waiting"):
                job["status"] = "running"
            elif event.get("type") == "done":
                job["status"] = "done"
            elif event.get("type") == "stopped":
                job["status"] = "stopped"
            elif event.get("type") == "error":
                job["status"] = "error"


@app.route("/")
def index():
    config_errors = validate_config()
    return render_template(
        "index.html",
        sender_email=SENDER_EMAIL or "not configured",
        delay_min=DELAY_MIN,
        delay_max=DELAY_MAX,
        config_ok=len(config_errors) == 0,
        config_errors=config_errors,
        default_subject=SUBJECT_TEMPLATE,
        default_html=HTML_TEMPLATE,
    )


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected."}), 400

    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Please upload a .csv file."}), 400

    content = file.read()
    receivers, errors = parse_csv(content)

    if not receivers:
        return jsonify({
            "error": "No valid recipients found in CSV.",
            "parse_errors": errors,
        }), 400

    job_id = _create_job(receivers, errors, file.filename)
    preview = [
        {
            "email": r["email"],
            "Name": r.get("Name", ""),
            "Company_Name": r.get("Company_Name", ""),
        }
        for r in receivers[:50]
    ]

    return jsonify({
        "job_id": job_id,
        "count": len(receivers),
        "preview": preview,
        "parse_errors": errors,
        "truncated": len(receivers) > 50,
    })


@app.route("/api/templates/<job_id>", methods=["GET", "POST"])
def templates(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404

        if request.method == "GET":
            return jsonify({
                "subject_template": job["subject_template"],
                "html_template": job["html_template"],
            })

        data = request.get_json(silent=True) or {}
        subject = data.get("subject_template")
        html = data.get("html_template")
        if not subject or not html:
            return jsonify({"error": "Subject and body are required."}), 400
        job["subject_template"] = subject
        job["html_template"] = html
        return jsonify({"ok": True})


@app.route("/api/send/<job_id>", methods=["POST"])
def send(job_id: str):
    global _active_job_id

    data = request.get_json(silent=True) or {}
    subject_override = data.get("subject_template")
    html_override = data.get("html_template")

    with _lock:
        if _active_job_id and _active_job_id != job_id:
            active = _jobs.get(_active_job_id)
            if active and active["status"] == "running":
                return jsonify({"error": "Another send is already running."}), 409

        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found. Upload CSV again."}), 404

        if job["status"] == "running":
            return jsonify({"error": "This job is already running."}), 409

        _active_job_id = job_id
        _stop_flags[job_id] = False
        job["status"] = "running"
        job["events"].clear()
        if subject_override:
            job["subject_template"] = subject_override
        if html_override:
            job["html_template"] = html_override
        subject_template = job["subject_template"]
        html_template = job["html_template"]

    receivers = job["receivers"]

    def worker() -> None:
        global _active_job_id
        summary = run_campaign(
            receivers,
            on_status=lambda e: _append_event(job_id, e),
            should_stop=lambda: _stop_flags.get(job_id, False),
            subject_template=subject_template,
            html_template=html_template,
            original_filename=job.get("filename", "recipients.csv")
        )
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["summary"] = summary
            if _active_job_id == job_id:
                _active_job_id = None

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/stop/<job_id>", methods=["POST"])
def stop(job_id: str):
    _stop_flags[job_id] = True
    return jsonify({"ok": True})


@app.route("/api/status/<job_id>")
def status(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        return jsonify({
            "status": job["status"],
            "events": list(job["events"]),
            "summary": job["summary"],
            "parse_errors": job["parse_errors"],
            "count": len(job["receivers"]),
        })


def main() -> None:
    print("\n  Renas Media Email Sender")
    print("  ─────────────────────────")
    print("  Open in your browser:")
    print("    http://localhost:5050")
    print("    http://<your-local-ip>:5050  (other devices on same network)")
    print("  ─────────────────────────\n")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)


if __name__ == "__main__":
    main()
