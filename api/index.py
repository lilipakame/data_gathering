import base64
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, redirect, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_app as core

app = Flask(__name__)


def _is_authenticated() -> bool:
    username = os.getenv("APP_USERNAME")
    password = os.getenv("APP_PASSWORD")
    if not username and not password:
        return True
    if not username or not password:
        raise RuntimeError("APP_USERNAME and APP_PASSWORD must both be set to enable Basic auth.")

    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    except Exception:
        return False
    user, _, pw = decoded.partition(":")
    return secrets.compare_digest(user, username) and secrets.compare_digest(pw, password)


def _auth_required() -> Response:
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Data Gathering"'})


def _form_values() -> dict[str, str]:
    return {
        "company_name": request.form.get("company_name", ""),
        "stock_code": request.form.get("stock_code", ""),
        "rss_url": request.form.get("rss_url", ""),
        "edinet_code": request.form.get("edinet_code", ""),
    }


def _redirect_home(message: str) -> Response:
    return redirect(f"/?message={quote(message)}", code=303)


@app.get("/")
def home():
    if not _is_authenticated():
        return _auth_required()
    message = request.args.get("message", "")
    items, _headers, indexes = core.list_watch_items()
    return Response(core.render_page(items, indexes, message=message), mimetype="text/html")


@app.post("/items")
def create_item():
    if not _is_authenticated():
        return _auth_required()
    worksheet = core.build_worksheet()
    headers = core.ensure_headers(worksheet)
    indexes = core.column_indexes(headers)
    missing = core.missing_required_columns(indexes)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    worksheet.append_row(core.values_for_row(headers, indexes, _form_values()), value_input_option="USER_ENTERED")
    return _redirect_home("Added")


@app.post("/items/<int:row_number>")
def update_item(row_number: int):
    if not _is_authenticated():
        return _auth_required()
    if row_number < 2:
        raise ValueError("Invalid row number")
    worksheet = core.build_worksheet()
    headers = core.ensure_headers(worksheet)
    indexes = core.column_indexes(headers)
    missing = core.missing_required_columns(indexes)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    core.update_editable_cells(worksheet, row_number, indexes, _form_values())
    return _redirect_home("Updated")


@app.post("/items/<int:row_number>/delete")
def delete_item(row_number: int):
    if not _is_authenticated():
        return _auth_required()
    if row_number < 2:
        raise ValueError("Invalid row number")
    core.build_worksheet().delete_rows(row_number)
    return _redirect_home("Deleted")


@app.errorhandler(Exception)
def handle_error(error: Exception):
    return Response(
        f"<h1>Error</h1><p>{core.escape(str(error))}</p><p><a href='/'>Back</a></p>",
        status=500,
        mimetype="text/html",
    )
