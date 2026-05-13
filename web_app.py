import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import gspread
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

SPREADSHEET_ID = os.getenv(
    "GOOGLE_SPREADSHEET_ID",
    "1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws",
)
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "list")
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "abiding-ascent-476815-q6-56a05b29f113.json",
)
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
APP_USERNAME = os.getenv("APP_USERNAME")
APP_PASSWORD = os.getenv("APP_PASSWORD")

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
DEFAULT_HEADERS = [
    "企業名",
    "銘柄コード",
    "URL",
    "Edinetコード",
    "決算予定日",
    "決算種類",
    "現在株価",
]
HEADER_ALIASES = {
    "company_name": ["企業名", "銘柄名", "会社名", "Issue Name", "表示名"],
    "stock_code": ["銘柄コード", "コード", "Code"],
    "rss_url": ["URL", "RSS", "RSS URL", "RSS_URL", "RSSリンク"],
    "edinet_code": ["Edinetコード", "EDINETコード", "EDINET"],
    "fiscal_date": ["決算予定日", "決算発表予定日", "決算日"],
    "fiscal_kind": ["決算種類", "決算種別", "種別"],
    "current_price": ["現在株価", "株価"],
}


@dataclass
class WatchItem:
    row_number: int
    company_name: str = ""
    stock_code: str = ""
    rss_url: str = ""
    edinet_code: str = ""
    fiscal_date: str = ""
    fiscal_kind: str = ""
    current_price: str = ""


def normalize_label(value: str) -> str:
    return re.sub(r"\s+", "", str(value).replace("\n", "")).lower()


def load_service_account_info(raw_value: str) -> dict:
    raw_value = raw_value.strip()
    if raw_value.startswith("export "):
        raw_value = raw_value.removeprefix("export ").strip()
    if "=" in raw_value and not raw_value.lstrip().startswith("{"):
        key, value = raw_value.split("=", 1)
        if key.strip() in {"GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_CREDENTIALS_JSON"}:
            raw_value = value.strip()
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {"'", '"'}:
        raw_value = raw_value[1:-1].strip()

    def normalize_private_key(credentials: dict) -> dict:
        private_key = str(credentials.get("private_key", "")).strip()
        if not private_key:
            raise ValueError("service account private_key is empty")
        private_key = private_key.replace("\\n", "\n")
        private_key = private_key.replace("\r\n", "\n").replace("\r", "\n")
        private_key = re.sub(r"-----BEGIN PRIVATE KEY-----\s*", "-----BEGIN PRIVATE KEY-----\n", private_key)
        private_key = re.sub(r"\s*-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----\n", private_key)

        begin = "-----BEGIN PRIVATE KEY-----"
        end = "-----END PRIVATE KEY-----"
        if begin in private_key and end in private_key:
            body = private_key.split(begin, 1)[1].split(end, 1)[0]
            body = "".join(body.split())
            if body:
                wrapped_body = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
                private_key = f"{begin}\n{wrapped_body}\n{end}\n"
        credentials["private_key"] = private_key
        return credentials

    def parse_json(value: str) -> dict:
        credentials = json.loads(value)
        if isinstance(credentials, str):
            credentials = json.loads(credentials)
        if not isinstance(credentials, dict):
            raise ValueError("service account value is not a JSON object")
        return normalize_private_key(credentials)

    def repair_private_key_newlines(value: str) -> str:
        match = re.search(r'("private_key"\s*:\s*")(.*?)("\s*,\s*"client_email")', value, re.S)
        if not match:
            return value
        private_key = match.group(2).replace("\\n", "\n")
        private_key = private_key.replace("\r\n", "\n").replace("\r", "\n")
        private_key = private_key.strip("\n").replace("\n", "\\n")
        return value[: match.start(2)] + private_key + value[match.end(2) :]

    try:
        return parse_json(raw_value)
    except (json.JSONDecodeError, ValueError):
        pass

    repaired_value = repair_private_key_newlines(raw_value)
    if repaired_value != raw_value:
        try:
            return parse_json(repaired_value)
        except (json.JSONDecodeError, ValueError):
            pass

    decoded = base64.b64decode("".join(raw_value.split()), validate=True).decode("utf-8")
    return parse_json(decoded)


def build_worksheet():
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_CREDENTIALS_JSON")
    if service_account_json:
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(
            load_service_account_info(service_account_json), SCOPES
        )
    else:
        credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, SCOPES)
    return gspread.authorize(credentials).open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)


def ensure_headers(worksheet) -> list[str]:
    values = worksheet.get_all_values()
    if not values:
        worksheet.append_row(DEFAULT_HEADERS, value_input_option="USER_ENTERED")
        return DEFAULT_HEADERS.copy()
    headers = values[0]
    if not any(cell.strip() for cell in headers):
        worksheet.update("A1:G1", [DEFAULT_HEADERS], value_input_option="USER_ENTERED")
        return DEFAULT_HEADERS.copy()
    return headers


def pick_header_index(headers: list[str], field_name: str) -> int | None:
    normalized_headers = [normalize_label(header) for header in headers]
    for alias in HEADER_ALIASES[field_name]:
        normalized_alias = normalize_label(alias)
        for index, header in enumerate(normalized_headers):
            if normalized_alias == header or normalized_alias in header:
                return index
    return None


def column_indexes(headers: list[str]) -> dict[str, int | None]:
    return {field_name: pick_header_index(headers, field_name) for field_name in HEADER_ALIASES}


def row_value(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def list_watch_items() -> tuple[list[WatchItem], list[str], dict[str, int | None]]:
    worksheet = build_worksheet()
    headers = ensure_headers(worksheet)
    indexes = column_indexes(headers)
    values = worksheet.get_all_values()[1:]
    items = [
        WatchItem(
            row_number=row_number,
            company_name=row_value(row, indexes["company_name"]),
            stock_code=row_value(row, indexes["stock_code"]),
            rss_url=row_value(row, indexes["rss_url"]),
            edinet_code=row_value(row, indexes["edinet_code"]),
            fiscal_date=row_value(row, indexes["fiscal_date"]),
            fiscal_kind=row_value(row, indexes["fiscal_kind"]),
            current_price=row_value(row, indexes["current_price"]),
        )
        for row_number, row in enumerate(values, start=2)
        if any(cell.strip() for cell in row)
    ]
    return items, headers, indexes


def values_for_row(headers: list[str], indexes: dict[str, int | None], form_values: dict[str, str]) -> list[str]:
    row = [""] * len(headers)
    for field_name, value in form_values.items():
        index = indexes[field_name]
        if index is not None:
            row[index] = value.strip()
    return row


def update_editable_cells(worksheet, row_number: int, indexes: dict[str, int | None], form_values: dict[str, str]) -> None:
    for field_name, value in form_values.items():
        index = indexes[field_name]
        if index is not None:
            worksheet.update_cell(row_number, index + 1, value.strip())


def missing_required_columns(indexes: dict[str, int | None]) -> list[str]:
    labels = {"rss_url": "URL"}
    return [label for field_name, label in labels.items() if indexes[field_name] is None]


def split_items(items: list[WatchItem]) -> tuple[list[WatchItem], list[WatchItem]]:
    stock_items = []
    rss_only_items = []
    for item in items:
        has_stock_identity = bool(item.stock_code or item.edinet_code)
        if has_stock_identity:
            stock_items.append(item)
        elif item.rss_url:
            rss_only_items.append(item)
        else:
            stock_items.append(item)
    return stock_items, rss_only_items


def row_form(item: WatchItem, *, rss_only: bool = False) -> str:
    title = item.company_name if item.company_name else ("RSSフィード" if rss_only else "")
    return f"""
<tr>
  <form method="post" action="/items/{item.row_number}">
    <td><input name="company_name" value="{escape(title)}" placeholder="企業名 or 表示名"></td>
    <td><input name="stock_code" value="{escape(item.stock_code)}" placeholder="7203"></td>
    <td><input name="rss_url" value="{escape(item.rss_url)}" placeholder="https://..."></td>
    <td><input name="edinet_code" value="{escape(item.edinet_code)}" placeholder="E02144"></td>
    <td class="ro">{escape(item.fiscal_date or "-")}</td>
    <td class="ro">{escape(item.fiscal_kind or "-")}</td>
    <td class="ro">{escape(item.current_price or "-")}</td>
    <td class="actions">
      <button type="submit" class="btn-save">保存</button>
  </form>
      <form method="post" action="/items/{item.row_number}/delete" onsubmit="return confirm('この行を削除しますか？');">
        <button type="submit" class="btn-del">削除</button>
      </form>
    </td>
</tr>
"""


def render_table(items: list[WatchItem], empty_label: str, *, rss_only: bool = False) -> str:
    if not items:
        return f'<p class="empty">{escape(empty_label)}</p>'
    rows = "".join(row_form(item, rss_only=rss_only) for item in items)
    return f"""
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>名称</th>
        <th>銘柄コード</th>
        <th>URL</th>
        <th>EDINET</th>
        <th>決算予定日</th>
        <th>決算種類</th>
        <th>現在株価</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


def render_page(items: list[WatchItem], indexes: dict[str, int | None], message: str = "") -> str:
    stock_items, rss_only_items = split_items(items)
    missing = missing_required_columns(indexes)
    message_html = f'<div class="notice">{escape(message)}</div>' if message else ""
    missing_html = ""
    if missing:
        missing_html = (
            '<div class="warning">シートのヘッダーに '
            + escape(", ".join(missing))
            + " が見つかりません。1行目の列名を確認してください。</div>"
        )

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>銘柄・RSS 管理</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="container">
    <header class="topbar">
      <h1>銘柄・RSS 管理</h1>
      <div class="meta">Spreadsheet: {escape(SPREADSHEET_ID)} / Sheet: {escape(WORKSHEET_NAME)}</div>
    </header>
    {message_html}
    {missing_html}

    <section class="panel">
      <h2>銘柄登録</h2>
      <form method="post" action="/items" class="quick-form">
        <input name="company_name" placeholder="企業名">
        <input name="stock_code" placeholder="銘柄コード">
        <input name="rss_url" placeholder="URL (RSS)">
        <input name="edinet_code" placeholder="EDINETコード">
        <button type="submit">追加</button>
      </form>
      <div class="hint">銘柄ウォッチ: {len(stock_items)}件</div>
      {render_table(stock_items, "銘柄データはまだありません。")}
    </section>

    <section class="panel">
      <h2>RSS単独（例: 頼さんノート）</h2>
      <form method="post" action="/items" class="quick-form">
        <input name="company_name" placeholder="表示名 (例: 頼さんノート)">
        <input name="stock_code" value="" placeholder="銘柄コード不要">
        <input name="rss_url" placeholder="URL (RSS)" required>
        <input name="edinet_code" value="" placeholder="EDINET不要">
        <button type="submit">RSS追加</button>
      </form>
      <div class="hint">RSS単独: {len(rss_only_items)}件</div>
      {render_table(rss_only_items, "RSS単独データはまだありません。", rss_only=True)}
    </section>
  </main>
</body>
</html>"""


class WatchlistHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self.is_authenticated():
            self.send_auth_required()
            return
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        query = parse_qs(parsed.query)
        message = query.get("message", [""])[0]
        try:
            items, _headers, indexes = list_watch_items()
            self.send_html(render_page(items, indexes, message=message))
        except Exception as error:
            self.send_error_page(error)

    def do_POST(self) -> None:
        if not self.is_authenticated():
            self.send_auth_required()
            return
        parsed = urlparse(self.path)
        try:
            form = self.read_form()
            if parsed.path == "/items":
                self.create_item(form)
                return

            update_match = re.fullmatch(r"/items/(\d+)", parsed.path)
            if update_match:
                self.update_item(int(update_match.group(1)), form)
                return

            delete_match = re.fullmatch(r"/items/(\d+)/delete", parsed.path)
            if delete_match:
                self.delete_item(int(delete_match.group(1)))
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        except Exception as error:
            self.send_error_page(error)

    def create_item(self, form: dict[str, str]) -> None:
        worksheet = build_worksheet()
        headers = ensure_headers(worksheet)
        indexes = column_indexes(headers)
        self.abort_if_missing_columns(indexes)
        if not any(v.strip() for v in form.values()):
            raise ValueError("空の行は追加できません。")
        worksheet.append_row(values_for_row(headers, indexes, form), value_input_option="USER_ENTERED")
        self.redirect_home("追加しました")

    def update_item(self, row_number: int, form: dict[str, str]) -> None:
        if row_number < 2:
            raise ValueError("不正な行番号です。")
        worksheet = build_worksheet()
        headers = ensure_headers(worksheet)
        indexes = column_indexes(headers)
        self.abort_if_missing_columns(indexes)
        update_editable_cells(worksheet, row_number, indexes, form)
        self.redirect_home("保存しました")

    def delete_item(self, row_number: int) -> None:
        if row_number < 2:
            raise ValueError("不正な行番号です。")
        build_worksheet().delete_rows(row_number)
        self.redirect_home("削除しました")

    def abort_if_missing_columns(self, indexes: dict[str, int | None]) -> None:
        missing = missing_required_columns(indexes)
        if missing:
            raise ValueError(f"必要な列がありません: {', '.join(missing)}")

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw_body, keep_blank_values=True)
        return {
            "company_name": parsed.get("company_name", [""])[0],
            "stock_code": parsed.get("stock_code", [""])[0],
            "rss_url": parsed.get("rss_url", [""])[0],
            "edinet_code": parsed.get("edinet_code", [""])[0],
        }

    def is_authenticated(self) -> bool:
        if not APP_USERNAME and not APP_PASSWORD:
            return True
        if not APP_USERNAME or not APP_PASSWORD:
            raise RuntimeError("APP_USERNAME and APP_PASSWORD must both be set to enable Basic auth.")
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
        except Exception:
            return False
        username, _, password = decoded.partition(":")
        return secrets.compare_digest(username, APP_USERNAME) and secrets.compare_digest(password, APP_PASSWORD)

    def send_auth_required(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Data Gathering"')
        self.end_headers()

    def send_html(self, body: str, status_code: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect_home(self, message: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?message={quote(message)}")
        self.end_headers()

    def send_error_page(self, error: Exception) -> None:
        body = "<h1>Error</h1>" f"<p>{escape(str(error))}</p>" '<p><a href="/">Back</a></p>'
        self.send_html(body, status_code=HTTPStatus.INTERNAL_SERVER_ERROR)


def run() -> None:
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), WatchlistHandler)
    print(f"銘柄・RSS 管理UIを起動しました: http://{APP_HOST}:{APP_PORT}")
    server.serve_forever()


CSS = """
:root {
  color-scheme: light;
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
body { margin: 0; background: #f5f7fa; color: #1e293b; }
.container { width: min(1320px, calc(100% - 20px)); margin: 10px auto 20px; }
.topbar { display: flex; justify-content: space-between; align-items: end; gap: 8px; margin: 0 0 10px; }
.topbar h1 { margin: 0; font-size: 22px; }
.meta { font-size: 12px; color: #64748b; overflow-wrap: anywhere; }
.panel { background: #fff; border: 1px solid #dbe3ec; border-radius: 8px; padding: 10px; margin-bottom: 12px; }
.panel h2 { margin: 0 0 8px; font-size: 16px; }
.quick-form {
  display: grid;
  grid-template-columns: 1.2fr .9fr 2fr .9fr auto;
  gap: 6px;
  margin-bottom: 8px;
}
input {
  width: 100%;
  box-sizing: border-box;
  height: 34px;
  border: 1px solid #c9d4df;
  border-radius: 6px;
  padding: 0 9px;
  font: inherit;
  font-size: 14px;
  background: #fff;
}
input:focus { outline: 2px solid #bfdbfe; border-color: #3b82f6; }
button {
  height: 34px;
  border: 0;
  border-radius: 6px;
  padding: 0 12px;
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
.quick-form button, .btn-save { background: #2563eb; color: #fff; }
.btn-del { background: #ffe4e6; color: #be123c; }
.hint { font-size: 12px; color: #475569; margin: 0 0 6px; }
.table-wrap { overflow-x: auto; border: 1px solid #dbe3ec; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; min-width: 980px; }
th, td { border-bottom: 1px solid #e7edf3; padding: 6px; vertical-align: middle; }
th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #f8fafc;
  font-size: 12px;
  color: #334155;
  text-align: left;
  white-space: nowrap;
}
td.ro { color: #64748b; font-size: 12px; white-space: nowrap; }
td.actions { width: 126px; white-space: nowrap; }
td.actions form { display: inline-block; margin: 0; }
td.actions button { width: 56px; margin-left: 4px; }
.notice, .warning { margin: 0 0 10px; border-radius: 8px; padding: 9px 10px; font-size: 13px; font-weight: 600; }
.notice { background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }
.warning { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
.empty { color: #64748b; font-size: 13px; padding: 8px; margin: 0; }
@media (max-width: 900px) {
  .topbar { display: block; }
  .topbar h1 { margin-bottom: 2px; }
  .quick-form { grid-template-columns: 1fr 1fr; }
  .quick-form button { grid-column: 1 / -1; }
}
"""


if __name__ == "__main__":
    run()
