import io
import json
import os
import re
from datetime import timedelta

import gspread
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from gspread.cell import Cell
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

SPREADSHEET_ID = "1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws"
WORKSHEET_NAME = "list"
JPX_PAGE = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "abiding-ascent-476815-q6-56a05b29f113.json")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")


def pick_column(columns, keywords):
    for col in columns:
        label = str(col)
        if any(k in label for k in keywords):
            return col
    raise ValueError(f"Column not found for {keywords}")


def pick_header_index(headers, candidates):
    for name in candidates:
        if name in headers:
            return headers.index(name)
    return None


def normalize_code(raw):
    if not raw:
        return None
    m = re.search(r"\d{4}", str(raw))
    return m.group(0).zfill(4) if m else None


def fetch_all_jpx_df():
    resp = requests.get(JPX_PAGE, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    base = "https://www.jpx.co.jp"
    xlsx_links = [
        base + a["href"]
        for a in soup.find_all("a", href=True)
        if a["href"].endswith(".xlsx")
    ]
    if not xlsx_links:
        raise RuntimeError("JPXページにxlsxリンクが見つかりませんでした")

    dfs = []
    for url in xlsx_links:
        try:
            file_resp = requests.get(url, timeout=30)
            file_resp.raise_for_status()
            df = pd.read_excel(io.BytesIO(file_resp.content), engine="openpyxl", skiprows=4)
            df.columns = [str(c).split("\n")[0].strip() for c in df.columns]
            dfs.append(df)
        except Exception as e:
            print(f"読み込み失敗: {url} ({e})")

    if not dfs:
        raise RuntimeError("xlsxを読み込めませんでした")

    return pd.concat(dfs, ignore_index=True)


def fetch_close_price(code: str):
    if not code:
        return None
    ticker = f"{code}.T"
    try:
        data = yf.download(ticker, period="5d", interval="1d", auto_adjust=False, progress=False)
        if data.empty:
            return None

        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if close.empty:
            return None

        return float(close.iloc[-1])
    except Exception:
        return None


def build_earnings_calendar():
    df = fetch_all_jpx_df()

    code_col = pick_column(df.columns, ["コード"])
    date_col = pick_column(df.columns, ["決算発表予定日", "決算予定日"])
    kind_col = pick_column(df.columns, ["種別", "種類"])

    df[code_col] = df[code_col].map(normalize_code)
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df[kind_col] = df[kind_col].astype(str).str.strip()

    calendar = (
        df.dropna(subset=[code_col])
        .drop_duplicates(subset=[code_col], keep="first")
        .set_index(code_col)[[date_col, kind_col]]
    )
    return calendar


def build_calendar_service():
    credentials = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def extract_http_reason(error: HttpError):
    try:
        payload = json.loads(error.content.decode("utf-8"))
        return payload["error"]["errors"][0].get("reason", "")
    except Exception:
        return ""


def event_exists(calendar_service, calendar_id, summary, date_str):
    next_date = (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    time_min = f"{date_str}T00:00:00Z"
    time_max = f"{next_date}T00:00:00Z"

    result = calendar_service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        q=summary,
        maxResults=20,
    ).execute()

    for event in result.get("items", []):
        start_date = event.get("start", {}).get("date")
        event_summary = (event.get("summary") or "").strip()
        if start_date == date_str and event_summary == summary:
            return True

    return False


def create_all_day_event(calendar_service, calendar_id, summary, date_str, description=None):
    next_date = (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    body = {
        "summary": summary,
        "start": {"date": date_str},
        "end": {"date": next_date},
    }
    if description:
        body["description"] = description
    calendar_service.events().insert(calendarId=calendar_id, body=body).execute()


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)

    gc = gspread.authorize(credentials)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)

    values = ws.get_all_values()
    if not values:
        return

    headers = values[0]
    try:
        code_idx = headers.index("銘柄コード")
        date_idx = headers.index("決算予定日")
        kind_idx = headers.index("決算種類")
        price_idx = headers.index("現在株価")
    except ValueError as e:
        raise RuntimeError("ヘッダーに『銘柄コード』『決算予定日』『決算種類』『現在株価』があるか確認してください") from e

    name_idx = pick_header_index(headers, ["企業名", "銘柄名", "会社名"])
    if name_idx is None:
        print("企業名カラム(企業名/銘柄名/会社名)が見つからないため、Calendar登録はスキップされます。")

    earnings_calendar = build_earnings_calendar()
    calendar_service = build_calendar_service()
    calendar_disabled = False
    created_events = 0

    updates = []
    for row_num, row in enumerate(values[1:], start=2):
        code = normalize_code(row[code_idx] if code_idx < len(row) else "")
        if not code:
            continue

        if code in earnings_calendar.index:
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

            if (not calendar_disabled) and company_name and date_str and isinstance(kind_val, str) and kind_val:
                summary = f"{company_name}{kind_val}"
                description = f"銘柄コード: {code}"
                try:
                    if not event_exists(calendar_service, CALENDAR_ID, summary, date_str):
                        create_all_day_event(
                            calendar_service=calendar_service,
                            calendar_id=CALENDAR_ID,
                            summary=summary,
                            date_str=date_str,
                            description=description,
                        )
                        created_events += 1
                except HttpError as e:
                    reason = extract_http_reason(e)
                    if reason == "accessNotConfigured":
                        calendar_disabled = True
                        print(
                            "Calendar APIが未有効です。"
                            "Google Cloudで Calendar API を有効化後に再実行してください。"
                        )
                    elif reason in {"notFound", "forbidden"}:
                        calendar_disabled = True
                        print(
                            f"Calendar ID '{CALENDAR_ID}' にアクセスできません。"
                            "カレンダー共有設定とGOOGLE_CALENDAR_IDを確認してください。"
                        )
                    else:
                        print(f"Calendar登録失敗: code={code}, date={date_str}, error={e}")

        close_price = fetch_close_price(code)
        if close_price is not None:
            updates.append(Cell(row=row_num, col=price_idx + 1, value=close_price))

    if updates:
        ws.update_cells(updates, value_input_option="USER_ENTERED")

    print(f"Google Calendarに {created_events} 件登録しました (calendar_id={CALENDAR_ID})")


if __name__ == "__main__":
    main()
