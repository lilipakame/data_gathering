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

#設定
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise RuntimeError("DISCORD_WEBHOOK_URL is not set. Add it to .env or your environment.")
# ここでスプレッドシートIDを指定（関数定義は下側の1つだけを使うように重複を削除）
spreadsheet_id = "1WlamXyzIj6GZAkU_lc8C0mTvMzwoHZk-R_HodUC3Sws"

# スプレッドシートからRSSリスト取得する関数
def get_urls_from_spreadsheet():
    # GoogleAPI認証
    # Google APIのスコープを設定
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

    # 認証情報を読み込む
    credentials = ServiceAccountCredentials.from_json_keyfile_name("abiding-ascent-476815-q6-56a05b29f113.json", scope)

    # Googleスプレッドシートに接続（gspreadを使用）
    client = gspread.authorize(credentials)
    # スプレッドシートを開く
    sh = client.open_by_key(spreadsheet_id)
    # 指定範囲の値を取得（A列の2行目以降）
    try:
        values = sh.worksheet('list').get("C2:C")
    except Exception:
        # フォールバック：列Cの値を取得してヘッダー行をスキップ
        values = [[v] for v in sh.worksheet('list').col_values(1)[1:]]
    flat_urls = [item[0] for item in values if item]
    return flat_urls

def get_company_names_from_spreadsheet():
    # GoogleAPI認証
    # Google APIのスコープを設定
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

    # 認証情報を読み込む
    credentials = ServiceAccountCredentials.from_json_keyfile_name("abiding-ascent-476815-q6-56a05b29f113.json", scope)

    # Googleスプレッドシートに接続（gspreadを使用）
    client = gspread.authorize(credentials)
    # スプレッドシートを開く
    sh = client.open_by_key(spreadsheet_id)
    values = sh.worksheet('list').get('A2:A')
    flat_names = [item[0] for item in values if item]
    return flat_names

# HTMLタグを削除する関数(補助関数)
def clean_html(text):
    text_without_tags = re.sub(r'<[^>]*>', ' ', text)
    cleaned_text = re.sub(r'\s+', ' ', text_without_tags)
    return cleaned_text.strip()

# メイン関数
def process_rss_feed():
    # スプレッドシートからRSSリストを取得
    urls = get_urls_from_spreadsheet()
    tokyo_tz = ZoneInfo('Asia/Tokyo')
    today_jst = datetime.now(tokyo_tz).date()
    
    # RSSフィードからURLリストを取得
    dfs = []
    for i, url in enumerate(urls):
        try:
            f = feedparser.parse(url)
            entries = f.get("entries", [])
            df = pd.json_normalize(entries)
        except:
            continue

        # Ensure expected columns exist to avoid KeyError
        for col in ['title', 'summary', 'link', 'published', 'get_date', 'updated']:
            if col not in df.columns:
                df[col] = ''

        # googleアラート用のURLを削除 (use regex=False to avoid warnings)
        df['link'] = df['link'].astype(str).str.replace('https://www.google.com/url?rct=j&sa=t&url=', '', regex=False)
        
        # 追加：feedparserの parsed 時刻を保持（UTC基準のstruct_time想定）
        # entries と df の順序は対応しているため、同じ順でカラムに入れる
        pub_parsed = [e.get('published_parsed') for e in entries]
        upd_parsed = [e.get('updated_parsed') for e in entries]
        df['published_parsed'] = pub_parsed
        df['updated_parsed']   = upd_parsed
        
        # タイトルと要約をクリーニング
        df['title'] = df['title'].astype(str).apply(clean_html)
        df['summary'] = df['summary'].astype(str).apply(clean_html)

        # Clean published; if missing/empty, fallback to get_date or updated
        df['published'] = df['published'].astype(str).apply(clean_html)
        # If published is empty, try get_date then updated
        df['published'] = df['published'].where(df['published'].str.strip() != '', df['get_date'].astype(str))
        df['published'] = df['published'].where(df['published'].str.strip() != '', df['updated'].astype(str))
        # Final cleanup in case fallback had HTML/extra spaces
        df['published'] = df['published'].astype(str).apply(clean_html)
        
        # 追加：UTC→JSTの日付に変換して“今日のみ”にフィルタ
        def to_jst_date(st):
            # st は time.struct_time 互換（Noneの可能性あり）
            try:
                if st:
                    # feedparserの *_parsed はUTC相当として扱い、JSTへ変換
                    dt_utc = datetime(*st[:6], tzinfo=timezone.utc)
                    return dt_utc.astimezone(tokyo_tz).date()
            except Exception:
                pass
            return None

        df['__pub_date_jst'] = df['published_parsed'].apply(to_jst_date)
        # published_parsed が無い場合は updated_parsed をフォールバック
        missing_mask = df['__pub_date_jst'].isna()
        df.loc[missing_mask, '__pub_date_jst'] = df.loc[missing_mask, 'updated_parsed'].apply(to_jst_date)

        # 今日（JST）のみ残す
        df = df[df['__pub_date_jst'] == today_jst].copy()

        # ヘルパーカラムは捨てる（最終返却スキーマは据え置き）
        df.drop(columns=['published_parsed', 'updated_parsed', '__pub_date_jst'], errors='ignore', inplace=True)

        #企業名をスプシから追加
        company_names = get_company_names_from_spreadsheet()
        df['company_name'] = company_names[i]
        dfs.append(df)

    if not dfs:
        # Return empty dataframe with expected columns
        return pd.DataFrame(columns=['company_name', 'title', 'summary', 'link', 'published'])

    df = pd.concat(dfs, ignore_index=True)

    # 必要なカラムだけ抽出
    # Make sure columns exist before selecting
    for col in ['company_name','title', 'summary', 'link', 'published']:
        if col not in df.columns:
            df[col] = ''
    df = df[['company_name','title', 'summary', 'link', 'published']]
    return df

# 実行
df_rss_list = process_rss_feed()

df_display = df_rss_list.copy()

# 改行や余計な空白を整理
df_display["company_name"] = df_display["company_name"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
df_display["title"] = df_display["title"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
df_display["link"] = df_display["link"].astype(str).str.strip()

from IPython.display import display
pd.set_option("display.max_colwidth", None)

# Discord送信用（タイトルとリンクをまとめたテキスト）
entries = [
    f"{row['company_name']}\n{row['title']}\n{row['link']}"
    for _, row in df_display.iterrows()
]
text = "\n\n".join(entries)

# Discordに送信
if text.strip():  # テキストが空でない場合のみ送信
    r = requests.post(DISCORD_WEBHOOK_URL, json={"content": text})