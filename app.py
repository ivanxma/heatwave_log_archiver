"""Web console for MySQL performance_schema.error_log archival."""
from __future__ import annotations

import os
import secrets
import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

from modules.archive_service import drop_partition, ensure_future_partitions, ensure_schema, fetch_archive_page, list_partitions, recent_rows, run_archive_cycle, selected_partitions_zip, truncate_partition
from modules.config import ArchiveConfig, _settings, save_settings
from modules.job_state import load_state, record as record_job_state
from modules.mysql_util import test_mysql_connection
from modules.profile_store import ensure_profile_store, get_profile_by_name, load_profiles, save_profile_from_form
from modules.session_store import ServerSessionStore

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("ERROR_ARCHIVER_WEB_SECRET", secrets.token_urlsafe(32)),
    SESSION_COOKIE_NAME="error_archiver_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
)


@app.after_request
def prevent_stale_html(response):
    """Keep authenticated screens current after a deployment or configuration change."""
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Error-Archiver-UI"] = "vault-first-login-v2"
    return response
PROFILE_STORE_PATH = Path(os.environ.get("ERROR_ARCHIVER_PROFILE_STORE", "profiles.json"))
SERVER_SESSIONS = ServerSessionStore(int(os.environ.get("ERROR_ARCHIVER_SESSION_TTL", "3600")))
# Navigation reuses a recent server-side health result. Login and database
# operations still establish their own live connections when they are needed.
SESSION_HEALTH_CHECK_SECONDS = max(1, int(os.environ.get("ERROR_ARCHIVER_SESSION_HEALTH_CHECK_SECONDS", "300")))
ensure_profile_store(PROFILE_STORE_PATH)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        record = SERVER_SESSIONS.get(session.get("connection_id"))
        if session.get("session_scope") != "error-log-archiver" or not record:
            session.clear()
            return redirect(url_for("login"))
        if SERVER_SESSIONS.health_check_due(session.get("connection_id"), SESSION_HEALTH_CHECK_SECONDS):
            try:
                test_mysql_connection(record["profile"], str(record["username"]), str(record["password"]))
                SERVER_SESSIONS.mark_healthy(session.get("connection_id"))
            except Exception:
                SERVER_SESSIONS.delete(session.get("connection_id"))
                session.clear()
                flash("The selected MySQL profile is no longer reachable. Please sign in again.", "error")
                return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if SERVER_SESSIONS.get(session.get("connection_id")):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        profile_name = request.form.get("profile_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        profile = get_profile_by_name(load_profiles(PROFILE_STORE_PATH), profile_name)
        if not profile or not username or not password:
            flash("Select a profile and enter MySQL credentials.", "error")
        else:
            try:
                test_mysql_connection(profile, username, password)
                session.clear()
                session["session_scope"] = "error-log-archiver"
                session["connection_id"] = SERVER_SESSIONS.create(profile_name, username, password, profile)
                return redirect(url_for("initial_setup") if not _settings().get("configured") else url_for("dashboard"))
            except Exception as exc:
                flash(f"Could not authenticate with the selected MySQL profile: {exc}", "error")
    return render_template("login.html", profiles=load_profiles(PROFILE_STORE_PATH), selected_profile=request.args.get("profile", ""))


@app.route("/profiles/new", methods=["GET", "POST"])
def create_profile():
    if request.method == "POST":
        try:
            name = save_profile_from_form(PROFILE_STORE_PATH, request.form)
            flash("Profile saved. Sign in using it.", "success")
            return redirect(url_for("login", profile=name))
        except Exception as exc:
            flash(str(exc), "error")
    return render_template("profile_form.html")


def profile_manager_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        record = SERVER_SESSIONS.get(session.get("connection_id"))
        if not record or not bool(record["profile"].get("profile_management")):
            flash("The selected profile is not authorized to manage profiles.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/initial-setup", methods=["GET", "POST"])
@profile_manager_required
def initial_setup():
    """First-login confirmation before non-secret settings and Vault references are saved."""
    record = SERVER_SESSIONS.get(session.get("connection_id"))
    profile = record["profile"]
    if request.method == "POST":
        if request.form.get("confirm_setup") != "yes":
            flash("Confirm archive setup before continuing.", "error")
        elif not request.form.get("source_secret_ocid", "").strip() or not request.form.get("archive_secret_ocid", "").strip():
            flash("Enter both OCI Vault Secret OCIDs. Passwords are retrieved by the VM at runtime and are not stored by this application.", "error")
        else:
            settings = _settings()
            archive_db = request.form.get("archive_db", "").strip()
            archive_table = request.form.get("archive_table", "").strip()
            settings.update({
                "enabled": True,
                "source_host": str(profile.get("host", "127.0.0.1")), "source_port": str(profile.get("port", 3306)),
                "source_socket": str(profile.get("socket", "")), "source_user": str(record["username"]), "source_secret_ocid": request.form.get("source_secret_ocid", "").strip(),
                "archive_host": request.form.get("archive_host", str(profile.get("host", "127.0.0.1"))).strip(),
                "archive_port": request.form.get("archive_port", str(profile.get("port", 3306))).strip(),
                "archive_socket": request.form.get("archive_socket", str(profile.get("socket", ""))).strip(),
                "archive_user": request.form.get("archive_user", str(record["username"])).strip(), "archive_secret_ocid": request.form.get("archive_secret_ocid", "").strip(),
                "archive_db": archive_db, "archive_table": archive_table, "log_type": request.form.get("log_type", "error_log").strip(),
                "schedule": request.form.get("schedule", "5min").strip(),
                "retention_months": request.form.get("retention_months", "12").strip(),
                "batch_size": request.form.get("batch_size", "5000").strip(),
            })
            settings.pop("source_password", None)
            settings.pop("archive_password", None)
            previous = _settings()
            try:
                save_settings(settings)
                config = ArchiveConfig.from_env(resolve_source_secret=False)
                ensure_schema(config)
                settings["configured"] = True
                save_settings(settings)
                flash(f"Archive database {config.archive_db} and table {config.archive_table} are configured.", "success")
                return redirect(url_for("dashboard"))
            except Exception as exc:
                save_settings(previous)
                flash(f"Setup did not complete: {exc}", "error")
    return render_dashboard("initial_setup.html", profile=profile, record=record, settings=_settings(), active_menu="setup")


@app.route("/logout")
def logout():
    SERVER_SESSIONS.delete(session.get("connection_id"))
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    if not _settings().get("configured"):
        return redirect(url_for("initial_setup"))
    # Rendering a dashboard shell must not contact OCI Vault. Credentials are
    # resolved only when the selected tab actually queries the archive database.
    config = ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False)
    page = max(1, request.args.get("page", 1, type=int))
    page_size = request.args.get("page_size", 50, type=int)
    selected_partition = request.args.get("partition", "")
    selected_source = request.args.get("source", "")
    selected_tab = request.args.get("tab", "summary")
    if selected_tab not in {"summary", "entries", "partitions"}:
        selected_tab = "summary"
    partitions, rows, total_rows, error = [], [], 0, None
    try:
        # Summary is status-only: do not contact the archive DB merely to open it.
        # Entries needs rows and partition choices; Partitions needs only metadata.
        if selected_tab == "entries":
            archive_config = ArchiveConfig.from_env(resolve_source_secret=False)
            partitions = list_partitions(archive_config)
            rows, total_rows = fetch_archive_page(archive_config, page, page_size, selected_partition, selected_source)
        elif selected_tab == "partitions":
            partitions = list_partitions(ArchiveConfig.from_env(resolve_source_secret=False))
    except Exception as exc:
        partitions, rows, total_rows, error = [], [], 0, str(exc)
    source_options = [*config.log_types, *(f"custom:{item['name']}" for item in config.custom_sources)]
    job_state = load_state()
    execution_page = max(1, request.args.get("execution_page", 1, type=int))
    execution_page_size = request.args.get("execution_page_size", 25, type=int)
    if execution_page_size not in {25, 50, 100}:
        execution_page_size = 25
    execution_total = len(job_state.get("history", []))
    execution_start = (execution_page - 1) * execution_page_size
    execution_history = list(job_state.get("history", []))[execution_start:execution_start + execution_page_size]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    activity = []
    for event in reversed(job_state.get("history", [])):
        try:
            when = datetime.fromisoformat(event["time"])
            if when >= cutoff:
                activity.append({"time": when.strftime("%H:%M"), "count": int(event.get("copied", 0) or 0), "status": event.get("status", "")})
        except (KeyError, ValueError, TypeError):
            continue
    return render_dashboard("dashboard.html", config=config, partitions=partitions, rows=rows, total_rows=total_rows, page=page, page_size=page_size, selected_partition=selected_partition, selected_source=selected_source, selected_tab=selected_tab, source_options=source_options, error=error, job_state=job_state, execution_history=execution_history, execution_total=execution_total, execution_page=execution_page, execution_page_size=execution_page_size, activity=activity, active_menu="archive")


@app.get("/archive-export.csv")
@login_required
def archive_export_csv():
    rows = recent_rows(ArchiveConfig.from_env(resolve_source_secret=False), 500)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=("event_time", "log_type", "payload", "archived_at"))
    writer.writeheader()
    writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=archived-log-entries.csv"})


@app.get("/partitions-export.csv")
@login_required
def partitions_export_csv():
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=("partition_name", "boundary", "table_rows", "data_length", "create_time"))
    writer.writeheader()
    writer.writerows(list_partitions(ArchiveConfig.from_env(resolve_source_secret=False)))
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=archive-partitions.csv"})


@app.get("/execution-history-export.csv")
@login_required
def execution_history_export_csv():
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=("time", "trigger", "status", "copied", "source_log_types", "partitions_added", "partitions_dropped", "detail"))
    writer.writeheader()
    for event in load_state().get("history", []):
        writer.writerow({
            "time": event.get("time", ""), "trigger": event.get("trigger", "Scheduled service"), "status": event.get("status", ""),
            "copied": event.get("copied", ""), "source_log_types": ", ".join(event.get("source_log_types", [])) or event.get("log_type", ""),
            "partitions_added": ", ".join(event.get("partitions_added", [])), "partitions_dropped": ", ".join(event.get("partitions_dropped", [])),
            "detail": event.get("error", "Completed"),
        })
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=archive-execution-history.csv"})


@app.route("/configuration", methods=["GET", "POST"])
@profile_manager_required
def configuration():
    fields = ("source_secret_ocid", "archive_secret_ocid", "source_host", "source_port", "source_user", "source_socket", "archive_host", "archive_port", "archive_user", "archive_socket", "archive_db", "archive_table", "retention_months", "batch_size", "worker_threads", "schedule")
    if request.method == "POST":
        original = _settings()
        settings = original.copy()
        section = request.form.get("configuration_section", "")
        if section == "policy":
            settings["enabled"] = request.form.get("enabled") == "on"
            settings["log_types"] = ",".join(request.form.getlist("log_types"))
        for field in fields:
            if field in request.form:
                settings[field] = request.form.get(field, "").strip()
        settings.pop("source_password", None)
        settings.pop("archive_password", None)
        try:
            save_settings(settings)
            ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False)  # validates non-secret values
            flash("Configuration saved. The next timer tick uses these settings.", "success")
            return redirect(url_for("configuration"))
        except Exception as exc:
            save_settings(original)
            flash(f"Configuration was not accepted: {exc}", "error")
    settings = _settings()
    try:
        config = ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False)
    except ValueError as exc:
        config = None
        flash(f"Existing configuration needs migration: {exc}. Edit or replace the affected mapping.", "error")
    return render_dashboard("configuration.html", settings=settings, config=config, source_connections=settings.get("source_connections", []), archive_connections=settings.get("archive_connections", []), source_tables=settings.get("source_tables", []), archive_tables=settings.get("archive_tables", []), source_mappings=settings.get("source_mappings", []), active_menu="configuration")


_ENTITY_FIELDS = {
    "source-connections": ("source_connections", ("name", "host", "port", "user", "secret_ocid", "socket")),
    "archive-connections": ("archive_connections", ("name", "host", "port", "user", "secret_ocid", "socket")),
    "source-tables": ("source_tables", ("name", "connection", "source", "timestamp_column")),
    "archive-tables": ("archive_tables", ("name", "connection", "archive_db", "archive_table")),
    "mappings": ("source_mappings", ("name", "source_table", "archive_table_ref", "enabled")),
}


@app.route("/configuration/<kind>/new", defaults={"index": -1}, methods=["GET", "POST"])
@app.route("/configuration/<kind>/<int:index>", methods=["GET", "POST"])
@profile_manager_required
def configuration_entity(kind: str, index: int):
    if kind not in _ENTITY_FIELDS:
        return redirect(url_for("configuration"))
    key, fields = _ENTITY_FIELDS[kind]
    settings = _settings(); items = list(settings.get(key, [])); item = items[index] if 0 <= index < len(items) else {}
    if request.method == "POST":
        candidate = {field: request.form.get(field, "").strip() for field in fields}
        if kind == "mappings":
            candidate["enabled"] = "true" if request.form.get("enabled") == "on" else "false"
        updated = items[:index] + [candidate] + items[index + 1:] if index >= 0 else [*items, candidate]
        settings[key] = updated
        try:
            save_settings(settings); ArchiveConfig.from_env(False, False)
            flash("Configuration record saved.", "success"); return redirect(url_for("configuration"))
        except Exception as exc:
            flash(str(exc), "error"); settings = _settings()
    return render_dashboard("entity_form.html", kind=kind, item=item, index=index, source_connections=settings.get("source_connections", []), archive_connections=settings.get("archive_connections", []), source_tables=settings.get("source_tables", []), archive_tables=settings.get("archive_tables", []), active_menu="configuration")


@app.post("/configuration/<kind>/<int:index>/delete")
@profile_manager_required
def configuration_entity_delete(kind: str, index: int):
    if kind in _ENTITY_FIELDS:
        key, _ = _ENTITY_FIELDS[kind]; settings = _settings(); items = list(settings.get(key, []))
        if 0 <= index < len(items):
            del items[index]; settings[key] = items; save_settings(settings); flash("Configuration record deleted.", "success")
    return redirect(url_for("configuration"))


@app.route("/custom-sources/<int:index>", methods=["GET", "POST"])
@app.route("/custom-sources/new", defaults={"index": -1}, methods=["GET", "POST"])
@profile_manager_required
def custom_source_form(index: int):
    settings = _settings()
    sources = list(settings.get("source_mappings", []))
    current = sources[index] if 0 <= index < len(sources) else {}
    if request.method == "POST":
        fields = ("name", "source", "timestamp_column", "source_host", "source_port", "source_user", "source_secret_ocid", "source_socket", "archive_host", "archive_port", "archive_user", "archive_secret_ocid", "archive_socket", "archive_db", "archive_table")
        item = {field: request.form.get(field, "").strip() for field in fields}
        if not item["name"]:
            flash("Custom source name is required.", "error")
        else:
            candidate = {**settings, "source_mappings": sources[:index] + [item] + sources[index + 1:] if index >= 0 else [*sources, item]}
            try:
                save_settings(candidate)
                ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False)
                flash("Custom source saved.", "success")
                return redirect(url_for("configuration"))
            except Exception as exc:
                save_settings(settings)
                flash(str(exc), "error")
    return render_dashboard("custom_source_form.html", source=current, index=index, settings=settings, config=ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False), active_menu="configuration")


@app.post("/custom-sources/<int:index>/delete")
@profile_manager_required
def custom_source_delete(index: int):
    settings = _settings()
    sources = list(settings.get("source_mappings", []))
    if 0 <= index < len(sources):
        del sources[index]
        settings["source_mappings"] = sources
        save_settings(settings)
        flash("Custom source deleted.", "success")
    return redirect(url_for("configuration"))


@app.route("/archive-setup", methods=["GET", "POST"])
@profile_manager_required
def archive_setup():
    """Dedicated archive-destination setup, including a remote MySQL archive host."""
    if request.method == "POST":
        if request.form.get("confirm_setup") != "yes":
            flash("Confirm archive schema setup before continuing.", "error")
        else:
            original = _settings()
            settings = original.copy()
            for field in ("archive_host", "archive_port", "archive_user", "archive_socket", "archive_db", "archive_table"):
                settings[field] = request.form.get(field, "").strip()
            settings["archive_secret_ocid"] = request.form.get("archive_secret_ocid", "").strip()
            settings.pop("archive_password", None)
            try:
                save_settings(settings)
                config = ArchiveConfig.from_env(resolve_source_secret=False)
                ensure_schema(config)
                settings["configured"] = True
                save_settings(settings)
                flash(f"Archive destination {config.archive_host}: {config.archive_db}.{config.archive_table} is ready.", "success")
                return redirect(url_for("dashboard"))
            except Exception as exc:
                save_settings(original)
                flash(f"Archive destination setup failed: {exc}", "error")
    return render_dashboard("archive_setup.html", settings=_settings(), config=ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False), active_menu="archive_setup")


@app.post("/run-now")
@profile_manager_required
def run_now():
    try:
        config = ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False)
        if not config.source_mappings:
            config = ArchiveConfig.from_env()
        result = run_archive_cycle(config)
        record_job_state("Succeeded", **result, schedule=config.schedule, log_type=config.log_type, trigger="Web: run now")
        flash(f"Archive cycle completed: {result['copied']} row(s) copied; {len(result['partitions_dropped'])} partition(s) dropped.", "success")
    except Exception as exc:
        record_job_state("Failed", error=str(exc), trigger="Web: run now")
        flash(f"Archive cycle failed: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/partitions/ensure")
@profile_manager_required
def partitions_ensure():
    try:
        ensure_schema(ArchiveConfig.from_env(resolve_source_secret=False))
        flash("Archive schema and future partitions are ready.", "success")
    except Exception as exc:
        flash(f"Partition maintenance failed: {exc}", "error")
    return redirect(url_for("dashboard", tab="partitions"))


@app.post("/partitions/prepare")
@profile_manager_required
def partitions_prepare():
    try:
        months = max(1, min(int(request.form.get("months_ahead", "2")), 24))
        added = ensure_future_partitions(ArchiveConfig.from_env(resolve_source_secret=False), months)
        flash(f"Future partition preparation completed; {len(added)} partition(s) added.", "success")
    except Exception as exc:
        flash(f"Future partition preparation failed: {exc}", "error")
    return redirect(url_for("dashboard", tab="partitions"))


@app.post("/partitions/<partition_name>/truncate")
@profile_manager_required
def partition_truncate(partition_name: str):
    try:
        truncate_partition(ArchiveConfig.from_env(resolve_source_secret=False), partition_name)
        flash(f"Partition {partition_name} was emptied.", "success")
    except Exception as exc:
        flash(f"Could not empty partition: {exc}", "error")
    return redirect(url_for("dashboard", tab="partitions"))


@app.post("/partitions/<partition_name>/drop")
@profile_manager_required
def partition_drop(partition_name: str):
    try:
        drop_partition(ArchiveConfig.from_env(resolve_source_secret=False), partition_name)
        flash(f"Partition {partition_name} was deleted.", "success")
    except Exception as exc:
        flash(f"Could not delete partition: {exc}", "error")
    return redirect(url_for("dashboard", tab="partitions"))


@app.post("/partitions/bulk")
@profile_manager_required
def partitions_bulk():
    action = request.form.get("action")
    names = request.form.getlist("partitions")
    if not names:
        flash("Select at least one monthly partition.", "error")
        return redirect(url_for("dashboard", tab="partitions"))
    try:
        operation = truncate_partition if action == "empty" else drop_partition if action == "delete" else None
        if not operation:
            raise ValueError("Select a valid partition action.")
        for name in names:
            operation(ArchiveConfig.from_env(resolve_source_secret=False), name)
        flash(f"{len(names)} partition(s) {('emptied' if action == 'empty' else 'deleted')}.", "success")
    except Exception as exc:
        flash(f"Partition action failed: {exc}", "error")
    return redirect(url_for("dashboard", tab="partitions"))


@app.post("/partitions/download")
@profile_manager_required
def partitions_download():
    names = request.form.getlist("partitions")
    if not names:
        flash("Select at least one monthly partition to download.", "error")
        return redirect(url_for("dashboard", tab="partitions"))
    try:
        payload = selected_partitions_zip(ArchiveConfig.from_env(resolve_source_secret=False), names)
        return Response(payload, mimetype="application/zip", headers={"Content-Disposition": "attachment; filename=selected-archive-partitions.zip"})
    except Exception as exc:
        flash(f"Partition download failed: {exc}", "error")
        return redirect(url_for("dashboard", tab="partitions"))


def render_dashboard(page_template: str, **context):
    record = SERVER_SESSIONS.get(session.get("connection_id"))
    shared = {**SERVER_SESSIONS.public_context(record), "can_manage_profiles": bool(record and record["profile"].get("profile_management")), "active_menu": "archive"}
    shared.update(context)
    return render_template(page_template, **shared)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")), threaded=True)
