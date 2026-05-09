import json
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
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "abiding-ascent-476815-q6-56a05b29f113.json",
)
SPREADSHEET_ID = "1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws"
RANGE_IN_LIST_SHEET = "B2:B"  # worksheet("list").get(...) に渡す範囲

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

# スプレッドシートからコード一覧を取得（list!B2:B）
def get_code_from_spreadsheet() -> list[str]:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if service_account_json:
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(service_account_json), scope
        )
    else:
        credentials = ServiceAccountCredentials.from_json_keyfile_name(
            CREDENTIALS_FILE, scope
        )
    client = gspread.authorize(credentials)
    ws = client.open_by_key(SPREADSHEET_ID).worksheet("list")
    values = ws.get(RANGE_IN_LIST_SHEET)  # [["7203"], ["6758"], ...]
    return [row[0].strip() for row in values if row and row[0].strip()]

# 当日(JST)公表分のみを取得（publishedをそのまま利用）
def process_code_feed() -> pd.DataFrame:
    tokyo_tz = ZoneInfo("Asia/Tokyo")
    today_jst = datetime.now(tokyo_tz).date()

    codes = get_code_from_spreadsheet()
    rows = []
    
    for c in codes:
        url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{c}.rss"
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue

        for entry in feed.get("entries", []):
            published_raw = entry.get("published")
            if not published_raw:
                continue
            # published は JST で提供される前提。tzinfo が無い場合は JST を付与するだけで変換はしない。
            try:
                dt_pub = parsedate_to_datetime(published_raw)
                if dt_pub.tzinfo is None:
                    dt_pub = dt_pub.replace(tzinfo=tokyo_tz)
                pub_date_jst = dt_pub.date()
            except Exception:
                # 解析できない場合はスキップ
                continue
            if pub_date_jst == today_jst:
                title = clean_html(str(entry.get("title", "")))
                link = clean_link(str(entry.get("link", "")))
                published = clean_html(published_raw)
                rows.append({"title": title, "link": link, "published": published})

            

    if not rows:
        return pd.DataFrame(columns=["title", "link", "published"])

    df = pd.DataFrame(rows, columns=["title", "link", "published"])
    df = df.drop_duplicates(subset=["title", "link"]).reset_index(drop=True)
    return df

# 実行
df_code_list = process_code_feed()

df_display = df_code_list.copy()

# 改行や余計な空白を整理
df_display["title"] = df_display["title"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
df_display["link"] = df_display["link"].astype(str).str.strip()

from IPython.display import display
pd.set_option("display.max_colwidth", None)

# Discord送信用（タイトルとリンクをまとめたテキスト）
entries = [
    f"{row['title']}\n{row['link']}"
    for _, row in df_display.iterrows()
]
text = "\n\n".join(entries)

# Discordに送信
if text.strip():  # テキストが空でない場合のみ送信
    r = requests.post(DISCORD_WEBHOOK_URL, json={"content": text})
