import re,os
import feedparser
import hashlib
from datetime import datetime, timezone
import pandas as pd
from google.auth import default
from googleapiclient.discovery import build
from google.oauth2 import service_account
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv; load_dotenv()
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs, unquote

# 設定
SPREADSHEET_ID = "1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws"
RANGE_IN_LIST_SHEET = "D2:D"  # worksheet("list").get(...) に渡す範囲

# HTMLタグ除去
def clean_html(text: str) -> str:
    text_without_tags = re.sub(r"<[^>]*>", " ", text or "")
    cleaned_text = re.sub(r"\s+", " ", text_without_tags)
    return cleaned_text.strip()

# GoogleリダイレクトURL除去
def clean_link(link: str) -> str:
    if not link:
        return ""
    try:
        p = urlparse(link)
        if "google.com" in p.netloc and p.path.startswith("/url"):
            q = parse_qs(p.query)
            for key in ("url", "q"):
                if key in q and q[key]:
                    return unquote(q[key][0])
    except Exception:
        pass
    return link

scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
credentials = ServiceAccountCredentials.from_json_keyfile_name(
        "abiding-ascent-476815-q6-56a05b29f113.json", scope
    )
client = gspread.authorize(credentials)
sheet = client.open_by_key(SPREADSHEET_ID).worksheet("list")

#コードと企業名を取得
# Read raw values to avoid get_all_records() header-duplication error
values = sheet.get_all_values()  # list of rows (each row is a list of cell strings)
if not values:
    records = []
else:
    headers = values[0]
    # Replace empty header names with fallback unique names to avoid duplicates
    normalized_headers = [h.strip() if h and h.strip() else f"_col_{i}" for i, h in enumerate(headers)]
    records = []
    for row_vals in values[1:]:
        row_dict = {normalized_headers[i]: (row_vals[i] if i < len(row_vals) else "") for i in range(len(normalized_headers))}
        records.append(row_dict)

# Map code->company name using the expected header names in the sheet
code_to_name = {
    str(row.get("Edinetコード", "")).strip(): str(row.get("企業名", "")).strip()
    for row in records
    if row.get("Edinetコード")
}
codes = set(code_to_name.keys())  


# EDINET API (ESE140206) に基づき、code リストに含まれる銘柄の本日提出書類を取得し PDF URL を生成
from zoneinfo import ZoneInfo

EDINET_API = os.getenv("EDINET_API_KEY")

# JST 今日の日付でドキュメント一覧を取得
jst_today = datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
list_endpoint = "https://disclosure.edinet-fsa.go.jp/api/v2/documents.json"
results = requests.get(list_endpoint, params={"date": jst_today, "type": 2, "Subscription-Key": EDINET_API})
results.raise_for_status()
results = results.json().get("results", [])

rows = []

for item in results:
    edicode1 = str(item.get("issuerEdinetCode") or "").strip()
    edicode2 = str(item.get("edinetCode") or "").strip()

    match_code = next((code for code in (edicode1, edicode2) if code and code in codes), None)
    if not match_code:
        continue

    doc_id = item.get("docID")
    if not doc_id:
        continue

    title = item.get("docDescription", "")
    issuer_name = item.get("filerName")
    pdf_url = (
        f"https://disclosure.edinet-fsa.go.jp/api/v2/documents/{doc_id}?type=1"
        f"&subscription-key={EDINET_API}"
    )
    #企業名をスプシから追加
    rows.append({"company_name": code_to_name.get(match_code, ""),
                  "code": match_code, 
                  "issuer_name": issuer_name,
                  "title": title, 
                  "pdf_url": pdf_url})
    
df_list = pd.DataFrame(rows, columns=["company_name", "code", "issuer_name", "title", "pdf_url"])
df_list.drop(columns=['code'], inplace=True)

df_display = df_list.copy()

# 改行や余計な空白を整理
df_display["company_name"] = df_display["company_name"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
df_display["pdf_url"] = df_display["pdf_url"].astype(str).str.strip()

from IPython.display import display
pd.set_option("display.max_colwidth", None)

# Discord送信用（タイトルとリンクをまとめたテキスト）
entries = [
    f"{row['company_name']}：" + f"{row['title']}\n提出者：{row['issuer_name']}\n{row['pdf_url']}"
    for _, row in df_display.iterrows()
]
text = "\n\n".join(entries)

# 環境変数がセットされていればそれを使い、なければ既存の変数を利用
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Discordに送信
if text.strip():  # テキストが空でない場合のみ送信
    r = requests.post(WEBHOOK_URL, json={"content": text})