import base64
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

    def parse_json(value: str) -> dict:
        credentials = json.loads(value)
        if isinstance(credentials, str):
            credentials = json.loads(credentials)
        if not isinstance(credentials, dict):
            raise ValueError("service account value is not a JSON object")
        return normalize_private_key(credentials)

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

    try:
        decoded = base64.b64decode("".join(raw_value.split()), validate=True).decode("utf-8")
        return parse_json(decoded)
    except Exception as error:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON must contain service account JSON, "
            "GOOGLE_SERVICE_ACCOUNT_JSON=service-account-json, "
            "or base64-encoded service account JSON."
        ) from error


def get_code_from_spreadsheet() -> list[str]:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if service_account_json:
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(
            load_service_account_info(service_account_json), scope
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
