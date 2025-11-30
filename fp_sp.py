import io
import os
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from gspread.cell import Cell
from google.oauth2.service_account import Credentials
from oauth2client.service_account import ServiceAccountCredentials
import yfinance as yf
from dotenv import load_dotenv
load_dotenv()

SPREADSHEET_ID = ("1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws")
WORKSHEET_NAME = ("list")
JPX_PAGE = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"


def pick_column(columns, keywords):
    for col in columns:
        label = str(col)
        if any(k in label for k in keywords):
            return col
    raise ValueError(f"Column not found for {keywords}")

def normalize_code(raw):
    if not raw:
        return None
    m = re.search(r"\d{4}", str(raw))
    return m.group(0).zfill(4) if m else None

# 最新のJPXのエクセルデータ取得
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
            print(f"→ 読み込み失敗: {url} ({e})")

    if not dfs:
        raise RuntimeError("xlsxを読み込めませんでした")

    return pd.concat(dfs, ignore_index=True)

#株価取得
def fetch_close_price(code: str):
    """銘柄コード4桁 → yfinanceで終値を取得（なければ None）"""
    if not code:
        return None
    ticker = f"{code}.T"
    try:
        data = yf.download(ticker, period="5d", interval="1d", progress=False)
        if data.empty:
            return None
        # 最新行の終値
        return float(data["Close"].iloc[-1])
    except Exception:
        return None


#データ成形
def build_calendar():
    df = fetch_all_jpx_df()  # 1件のみなら fetch_latest_jpx_df でもOK

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

def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

    # 認証情報を読み込む
    credentials = ServiceAccountCredentials.from_json_keyfile_name("abiding-ascent-476815-q6-56a05b29f113.json", scope)
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

    calendar = build_calendar()

    updates = []
    for row_num, row in enumerate(values[1:], start=2):
        code = normalize_code(row[code_idx] if code_idx < len(row) else "")
        if not code:
            continue

        # 決算日・種別の更新
        if code in calendar.index:
            date_val, kind_val = calendar.loc[code]
            if not pd.isna(date_val):
                updates.append(Cell(row=row_num, col=date_idx + 1, value=date_val.strftime("%Y-%m-%d")))
            if isinstance(kind_val, str) and kind_val:
                updates.append(Cell(row=row_num, col=kind_idx + 1, value=kind_val))

        # 株価の更新
        close_price = fetch_close_price(code)
        if close_price is not None:
            updates.append(Cell(row=row_num, col=price_idx + 1, value=close_price))

    if updates:
        ws.update_cells(updates, value_input_option="USER_ENTERED")



if __name__ == "__main__":
    main()
