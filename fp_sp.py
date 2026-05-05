import argparse
import io
import json
import os
import re
import sys
import time
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import gspread
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from gspread.cell import Cell
from gspread.exceptions import APIError

load_dotenv()

SPREADSHEET_ID = os.getenv(
    "GOOGLE_SPREADSHEET_ID",
    "1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws",
)
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "list")
JPX_PAGE = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "abiding-ascent-476815-q6-56a05b29f113.json",
)
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar",
]

RETRYABLE_GOOGLE_STATUS_CODES = {429, 500, 502, 503, 504}


def normalize_label(value):
    return re.sub(r"\s+", "", str(value).replace("\n", ""))


def pick_column(columns, candidates):
    normalized = {col: normalize_label(col) for col in columns}
    for candidate in candidates:
        candidate = normalize_label(candidate)
        for col, label in normalized.items():
            if candidate in label:
                return col
    raise ValueError(f"列が見つかりません: {candidates} / columns={list(columns)}")


def pick_header_index(headers, candidates):
    normalized = [normalize_label(header) for header in headers]
    for candidate in candidates:
        candidate = normalize_label(candidate)
        for i, header in enumerate(normalized):
            if candidate in header:
                return i
    return None


def normalize_code(raw):
    if raw in (None, ""):
        return None
    match = re.search(r"\d{4}", str(raw))
    return match.group(0).zfill(4) if match else None


def fetch_all_jpx_df():
    response = requests.get(JPX_PAGE, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    excel_links = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = urlparse(href).path.lower()
        if not path.endswith((".xlsx", ".xls")):
            continue

        url = urljoin(JPX_PAGE, href)
        if url in seen_links:
            continue

        excel_links.append(url)
        seen_links.add(url)

    if not excel_links:
        raise RuntimeError("JPXページにExcelリンクが見つかりませんでした")

    dfs = []
    for url in excel_links:
        try:
            file_response = requests.get(url, timeout=30)
            file_response.raise_for_status()
            suffix = urlparse(url).path.lower().rsplit(".", 1)[-1]
            engine = "openpyxl" if suffix == "xlsx" else "xlrd"
            df = pd.read_excel(
                io.BytesIO(file_response.content),
                engine=engine,
                skiprows=4,
            )
            df.columns = [str(col).split("\n")[0].strip() for col in df.columns]
            df = df.dropna(how="all")
            dfs.append(df)
            print(f"JPX Excel読込: {url} ({len(df)} rows)")
        except Exception as error:
            print(f"JPX Excel読込失敗: {url} ({error})")

    if not dfs:
        raise RuntimeError("JPXのExcelを読み込めませんでした")

    return pd.concat(dfs, ignore_index=True)


def fetch_close_price(code):
    if not code:
        return None

    try:
        data = yf.download(
            f"{code}.T",
            period="5d",
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
        if data.empty:
            return None

        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if close.empty:
            return None

        return float(close.iloc[-1])
    except Exception as error:
        print(f"株価取得失敗: code={code}, error={error}")
        return None


def build_earnings_calendar():
    df = fetch_all_jpx_df()

    code_col = pick_column(df.columns, ["コード", "Code"])
    date_col = pick_column(df.columns, ["決算発表予定日", "Scheduled Dates"])
    kind_col = pick_column(df.columns, ["種別", "Fiscal Year/Quarter"])

    df[code_col] = df[code_col].map(normalize_code)
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df[kind_col] = df[kind_col].astype(str).str.strip()

    return (
        df.dropna(subset=[code_col])
        .sort_values(date_col, na_position="last")
        .drop_duplicates(subset=[code_col], keep="first")
        .set_index(code_col)[[date_col, kind_col]]
    )


def build_google_credentials():
    return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)


def build_calendar_service(credentials):
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def validate_calendar_target(credentials):
    if CALENDAR_ID != "primary":
        return

    service_account_email = getattr(credentials, "service_account_email", "")
    raise RuntimeError(
        "GOOGLE_CALENDAR_IDが未設定です。サービスアカウント認証で'primary'を使っても、"
        "あなた個人のGoogleカレンダーには登録されません。"
        " 個人カレンダーの設定画面にある「カレンダーID」またはGmailアドレスを"
        ".envのGOOGLE_CALENDAR_IDに設定してください。"
        f" 共有先サービスアカウント: {service_account_email}"
    )


def google_error_status(error):
    if isinstance(error, HttpError):
        return getattr(error.resp, "status", None)

    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code:
        return status_code

    match = re.search(r"\[(\d{3})\]", str(error))
    return int(match.group(1)) if match else None


def with_google_retries(label, operation, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (APIError, HttpError) as error:
            status = google_error_status(error)
            if status not in RETRYABLE_GOOGLE_STATUS_CODES or attempt == attempts:
                raise RuntimeError(f"{label}に失敗しました: {error}") from error

            wait_seconds = min(2 ** (attempt - 1), 30)
            print(f"{label}が一時エラー({status})でした。{wait_seconds}秒後に再試行します ({attempt}/{attempts})")
            time.sleep(wait_seconds)


def build_worksheet(credentials):
    gc = gspread.authorize(credentials)
    return with_google_retries(
        "スプレッドシート取得",
        lambda: gc.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME),
    )


def extract_http_reason(error):
    try:
        payload = json.loads(error.content.decode("utf-8"))
        return payload["error"]["errors"][0].get("reason", "")
    except Exception:
        return ""


def event_exists(calendar_service, calendar_id, summary, date_str, code):
    next_date = (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    result = with_google_retries(
        "Calendar既存イベント確認",
        lambda: (
            calendar_service.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{date_str}T00:00:00+09:00",
                timeMax=f"{next_date}T00:00:00+09:00",
                singleEvents=True,
                maxResults=250,
            )
            .execute()
        ),
    )

    for event in result.get("items", []):
        start_date = event.get("start", {}).get("date")
        event_summary = (event.get("summary") or "").strip()
        description = event.get("description") or ""
        if start_date == date_str and event_summary == summary:
            return True
        if start_date == date_str and f"銘柄コード: {code}" in description:
            return True

    return False


def find_existing_event(calendar_service, calendar_id, summary, date_str, code):
    next_date = (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")

    same_day_events = with_google_retries(
        "Calendar既存イベント確認",
        lambda: (
            calendar_service.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{date_str}T00:00:00+09:00",
                timeMax=f"{next_date}T00:00:00+09:00",
                singleEvents=True,
                maxResults=250,
            )
            .execute()
        ),
    ).get("items", [])

    for event in same_day_events:
        event_summary = (event.get("summary") or "").strip()
        description = event.get("description") or ""
        if event_summary == summary or str(code) in description:
            return event

    return None


def create_all_day_event(calendar_service, calendar_id, summary, date_str, code, description=None):
    next_date = (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    body = {
        "summary": summary,
        "start": {"date": date_str},
        "end": {"date": next_date},
        "description": description or f"銘柄コード: {code}",
    }
    with_google_retries(
        "Calendarイベント登録",
        lambda: calendar_service.events().insert(calendarId=calendar_id, body=body).execute(),
    )


def get_calendar_event_sync_status(calendar_service, calendar_id, summary, date_str, code, description):
    next_date = (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    body = {
        "summary": summary,
        "start": {"date": date_str},
        "end": {"date": next_date},
        "description": description,
    }

    existing = find_existing_event(calendar_service, calendar_id, summary, date_str, code)
    if not existing:
        return "created", None, body

    return "skipped", existing, body


def upsert_all_day_event(calendar_service, calendar_id, summary, date_str, code, description):
    status, existing, body = get_calendar_event_sync_status(
        calendar_service=calendar_service,
        calendar_id=calendar_id,
        summary=summary,
        date_str=date_str,
        code=code,
        description=description,
    )

    if status == "created":
        with_google_retries(
            "Calendarイベント登録",
            lambda: calendar_service.events().insert(calendarId=calendar_id, body=body).execute(),
        )
        return status

    if status == "skipped":
        return status

    return status


def explain_google_auth_error(error):
    message = str(error)
    if "Invalid JWT Signature" in message:
        return (
            "Google認証に失敗しました: サービスアカウント鍵の署名が無効です。"
            " Google Cloudで新しいJSONキーを発行し、GOOGLE_CREDENTIALS_FILEを差し替えてください。"
        )
    return f"Google認証に失敗しました: {error}"


def main(dry_run=False):
    credentials = build_google_credentials()
    try:
        ws = build_worksheet(credentials)
        values = with_google_retries("スプレッドシート読込", ws.get_all_values)
    except RefreshError as error:
        raise RuntimeError(explain_google_auth_error(error)) from error

    if not values:
        print("スプレッドシートにデータがありません")
        return

    headers = values[0]
    code_idx = pick_header_index(headers, ["銘柄コード", "コード", "Code"])
    date_idx = pick_header_index(headers, ["決算予定日", "決算発表予定日", "決算予想日"])
    kind_idx = pick_header_index(headers, ["決算種類", "決算種別", "種別"])
    price_idx = pick_header_index(headers, ["現在株価", "株価"])
    name_idx = pick_header_index(headers, ["企業名", "銘柄名", "会社名", "Issue Name"])

    missing = [
        name
        for name, idx in [
            ("銘柄コード", code_idx),
            ("決算予定日", date_idx),
            ("決算種類", kind_idx),
            ("現在株価", price_idx),
        ]
        if idx is None
    ]
    if missing:
        raise RuntimeError(f"スプレッドシートのヘッダーに必要列がありません: {missing}")

    if name_idx is None:
        print("会社名の列が見つからないため、Google Calendar登録はスキップします")

    earnings_calendar = build_earnings_calendar()
    calendar_service = build_calendar_service(credentials)
    validate_calendar_target(credentials)
    calendar_disabled = False
    created_events = 0
    updated_events = 0
    skipped_events = 0
    matched_rows = 0

    updates = []
    for row_num, row in enumerate(values[1:], start=2):
        code = normalize_code(row[code_idx] if code_idx < len(row) else "")
        if not code:
            continue

        if code in earnings_calendar.index:
            matched_rows += 1
            date_val, kind_val = earnings_calendar.loc[code]
            date_str = None

            if not pd.isna(date_val):
                date_str = date_val.strftime("%Y-%m-%d")
                updates.append(Cell(row=row_num, col=date_idx + 1, value=date_str))

            if isinstance(kind_val, str) and kind_val:
                updates.append(Cell(row=row_num, col=kind_idx + 1, value=kind_val))

            company_name = ""
            if name_idx is not None and name_idx < len(row):
                company_name = str(row[name_idx]).strip()

            if not calendar_disabled and company_name and date_str and isinstance(kind_val, str) and kind_val:
                summary = f"{company_name} {kind_val}"
                description = f"銘柄コード: {code}\n取得元: JPX 決算発表予定日"
                try:
                    if dry_run:
                        status, _, _ = get_calendar_event_sync_status(
                            calendar_service=calendar_service,
                            calendar_id=CALENDAR_ID,
                            summary=summary,
                            date_str=date_str,
                            code=code,
                            description=description,
                        )
                    else:
                        status = upsert_all_day_event(
                            calendar_service=calendar_service,
                            calendar_id=CALENDAR_ID,
                            summary=summary,
                            date_str=date_str,
                            code=code,
                            description=description,
                        )

                    if status == "created":
                        created_events += 1
                    elif status == "updated":
                        updated_events += 1
                    else:
                        skipped_events += 1
                except RefreshError as error:
                    raise RuntimeError(explain_google_auth_error(error)) from error
                except HttpError as error:
                    reason = extract_http_reason(error)
                    if reason == "accessNotConfigured":
                        calendar_disabled = True
                        print("Calendar APIが無効です。Google CloudでCalendar APIを有効化してください。")
                    elif reason in {"notFound", "forbidden"}:
                        calendar_disabled = True
                        print(
                            f"Calendar ID '{CALENDAR_ID}' にアクセスできません。"
                            " カレンダー共有設定とGOOGLE_CALENDAR_IDを確認してください。"
                        )
                    else:
                        print(f"Calendar登録失敗: code={code}, date={date_str}, error={error}")

        close_price = fetch_close_price(code)
        if close_price is not None:
            updates.append(Cell(row=row_num, col=price_idx + 1, value=close_price))

    if updates and not dry_run:
        with_google_retries(
            "スプレッドシート更新",
            lambda: ws.update_cells(updates, value_input_option="USER_ENTERED"),
        )

    mode = "DRY RUN: " if dry_run else ""
    print(
        f"{mode}JPX一致 {matched_rows} 行 / シート更新 {len(updates)} セル / "
        f"Calendar作成 {created_events} 件 / Calendar更新 {updated_events} 件 / Calendar既存スキップ {skipped_events} 件"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="スプレッドシートとカレンダーに書き込まず確認します")
    args = parser.parse_args()
    try:
        main(dry_run=args.dry_run)
    except RuntimeError as error:
        print(error)
        sys.exit(1)
