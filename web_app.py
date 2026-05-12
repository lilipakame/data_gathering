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
    "RSS",
    "Edinetコード",
    "決算予定日",
    "決算種類",
    "現在株価",
]
HEADER_ALIASES = {
    "company_name": ["企業名", "銘柄名", "会社名", "Issue Name"],
    "stock_code": ["銘柄コード", "コード", "Code"],
    "rss_url": ["RSS", "RSS URL", "RSS_URL", "RSSリンク"],
    "edinet_code": ["Edinetコード", "EDINETコード", "EDINET"],
    "fiscal_date": ["決算予定日", "決算発表予定日", "決算予想日"],
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
    labels = {
        "company_name": "企業名",
        "stock_code": "銘柄コード",
        "rss_url": "RSS",
        "edinet_code": "Edinetコード",
    }
    return [label for field_name, label in labels.items() if indexes[field_name] is None]


def input_field(name: str, label: str, placeholder: str, value: str = "", required: bool = False) -> str:
    required_attr = " required" if required else ""
    return (
        f'<label><span>{escape(label)}</span>'
        f'<input name="{escape(name)}" value="{escape(value)}" placeholder="{escape(placeholder)}"{required_attr}></label>'
    )


def render_item(item: WatchItem) -> str:
    return f"""<article class="item-card">
  <form method="post" action="/items/{item.row_number}" class="grid-form compact">
    <div class="row-title">
      <strong>{escape(item.company_name or "名称未設定")}</strong>
      <span>#{item.row_number}</span>
    </div>
    {input_field("company_name", "企業名", "企業名", item.company_name, required=True)}
    {input_field("stock_code", "銘柄コード", "銘柄コード", item.stock_code)}
    {input_field("rss_url", "RSS URL", "RSS URL", item.rss_url)}
    {input_field("edinet_code", "EDINETコード", "EDINETコード", item.edinet_code)}
    <div class="readonly">
      <span>決算予定日: {escape(item.fiscal_date or "-")}</span>
      <span>決算種類: {escape(item.fiscal_kind or "-")}</span>
      <span>現在株価: {escape(item.current_price or "-")}</span>
    </div>
    <div class="actions">
      <button type="submit">保存</button>
    </div>
  </form>
  <form method="post" action="/items/{item.row_number}/delete" class="delete-form">
    <button type="submit" onclick="return confirm('この行を削除しますか？')">削除</button>
  </form>
</article>"""


def render_page(items: list[WatchItem], indexes: dict[str, int | None], message: str = "") -> str:
    missing = missing_required_columns(indexes)
    rows_html = "".join(render_item(item) for item in items)
    message_html = f'<div class="notice">{escape(message)}</div>' if message else ""
    missing_html = ""
    if missing:
        missing_html = (
            '<div class="warning">スプレッドシートのヘッダーに '
            + escape("、".join(missing))
            + " が見つかりません。既存の取得処理に合わせた列名にしてください。</div>"
        )
    if not rows_html:
        rows_html = '<p class="empty">まだ登録がありません。下のフォームから追加してください。</p>'

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
    <header class="hero">
      <p class="eyebrow">Data Gathering</p>
      <h1>銘柄・RSS 管理</h1>
      <p>既存のGoogleスプレッドシートをデータ置き場にしたまま、スマホから銘柄コード・銘柄名・RSS・EDINETコードを追加できます。</p>
    </header>
    {message_html}
    {missing_html}
    <section class="card">
      <h2>新規追加</h2>
      <form method="post" action="/items" class="grid-form">
        {input_field("company_name", "企業名", "例：トヨタ自動車", required=True)}
        {input_field("stock_code", "銘柄コード", "例：7203")}
        {input_field("rss_url", "RSS URL", "https://...")}
        {input_field("edinet_code", "EDINETコード", "例：E02144")}
        <button type="submit">追加する</button>
      </form>
    </section>
    <section class="list-header">
      <h2>登録済みリスト</h2>
      <span>{len(items)}件</span>
    </section>
    <section class="items">{rows_html}</section>
    <footer>Spreadsheet: {escape(SPREADSHEET_ID)} / Worksheet: {escape(WORKSHEET_NAME)}</footer>
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
        worksheet.append_row(values_for_row(headers, indexes, form), value_input_option="USER_ENTERED")
        self.redirect_home("追加しました")

    def update_item(self, row_number: int, form: dict[str, str]) -> None:
        if row_number < 2:
            raise ValueError("更新できない行番号です")
        worksheet = build_worksheet()
        headers = ensure_headers(worksheet)
        indexes = column_indexes(headers)
        self.abort_if_missing_columns(indexes)
        update_editable_cells(worksheet, row_number, indexes, form)
        self.redirect_home("保存しました")

    def delete_item(self, row_number: int) -> None:
        if row_number < 2:
            raise ValueError("削除できない行番号です")
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
        body = (
            "<h1>エラー</h1>"
            f"<p>{escape(str(error))}</p>"
            '<p><a href="/">戻る</a></p>'
        )
        self.send_html(body, status_code=HTTPStatus.INTERNAL_SERVER_ERROR)


def run() -> None:
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), WatchlistHandler)
    print(f"銘柄・RSS 管理UIを起動しました: http://{APP_HOST}:{APP_PORT}")
    server.serve_forever()


CSS = """
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
body { margin: 0; background: #eef3f8; color: #102033; }
.container { width: min(960px, calc(100% - 28px)); margin: 0 auto; padding: 20px 0 40px; }
.hero { background: linear-gradient(135deg, #0f766e, #2563eb); color: white; border-radius: 26px; padding: 28px; box-shadow: 0 16px 40px rgba(37, 99, 235, .22); }
.hero h1 { margin: 4px 0 10px; font-size: clamp(28px, 8vw, 44px); }
.hero p { margin: 0; line-height: 1.7; }
.eyebrow { opacity: .8; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.card, .item-card { background: white; border-radius: 22px; padding: 18px; margin-top: 16px; box-shadow: 0 8px 24px rgba(16, 32, 51, .08); }
.card h2, .list-header h2 { margin: 0 0 14px; }
.grid-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.grid-form label { display: grid; gap: 6px; font-size: 13px; font-weight: 700; color: #42526b; }
.grid-form input { border: 1px solid #d8e0ea; border-radius: 14px; padding: 14px; font: inherit; font-size: 16px; background: #fbfdff; }
.grid-form input:focus { outline: 3px solid rgba(37, 99, 235, .18); border-color: #2563eb; }
button { border: 0; border-radius: 14px; padding: 14px 18px; font: inherit; font-weight: 800; background: #2563eb; color: white; cursor: pointer; }
button:hover { filter: brightness(.95); }
.grid-form > button { align-self: end; }
.notice, .warning { margin-top: 14px; border-radius: 16px; padding: 14px 16px; font-weight: 700; }
.notice { background: #dcfce7; color: #166534; }
.warning { background: #fef3c7; color: #92400e; }
.list-header { display: flex; align-items: center; justify-content: space-between; margin: 24px 4px 8px; }
.list-header span { background: white; border-radius: 999px; padding: 8px 12px; font-weight: 800; }
.row-title { grid-column: 1 / -1; display: flex; justify-content: space-between; align-items: center; }
.row-title strong { font-size: 20px; }
.row-title span { color: #667085; font-weight: 700; }
.readonly { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px; color: #475467; font-size: 13px; }
.readonly span { background: #f2f4f7; border-radius: 999px; padding: 7px 10px; }
.actions { display: flex; gap: 8px; align-items: end; }
.delete-form { margin-top: 10px; }
.delete-form button { background: #fff1f2; color: #be123c; width: 100%; }
.empty { background: white; border-radius: 18px; padding: 22px; color: #667085; }
footer { color: #667085; font-size: 12px; text-align: center; margin-top: 28px; overflow-wrap: anywhere; }
@media (max-width: 720px) {
  .container { width: min(100% - 20px, 960px); padding-top: 10px; }
  .hero { padding: 22px; border-radius: 22px; }
  .grid-form { grid-template-columns: 1fr; }
  .grid-form.compact { gap: 10px; }
  button { width: 100%; }
  .card, .item-card { padding: 16px; border-radius: 20px; }
}
"""


if __name__ == "__main__":
    run()
