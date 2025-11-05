import os
import requests
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables from a local .env file (if present)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env or your environment.")
if not WEBHOOK_URL:
    raise RuntimeError("DISCORD_WEBHOOK_URL is not set. Add it to .env or your environment.")

DEFAULT_PROMPT = """あなたは厳格なマーケット記者。ブラウズ機能を使い、今日{{date_yyyy-mm-dd}}の出来事のみを対象に、日本と世界の「株式関連ニュースと値動き」を一次/信頼ソースで確認し要約してください。公開時刻と実際の発生日のズレに注意し、誤りや噂は除外。不明な点は「不明」と明記。

必須観点：
1) 日本株の全体像：日経平均・TOPIX・東証グロース指数の終値/騰落率、売買代金の大小、主因（セクター寄与上位、為替(USD/JPY)、国内外金利、米株/中国市況、材料/決算/政策）。
2) 目立つ値動き：国内個別5–10件＋他資産（金・原油・為替・金利・主要ETF/仮想通貨等から2–3件）。銘柄/ティッカー、騰落率(終値基準)と理由（会社発表、報道、アナリスト、需給・テクニカル）。※「特徴的」= ±5%以上や年初来高安/出来高急増など。
3) その日の重要イベント（経済指標、政策、決算、IPO/増資等）最大5件。
4) 明日の注目材料を1–2件（文字数に余裕がある場合のみ）。

出典は各行末に[媒体名/日時]で簡潔に（例：[Reuters 21:05JST]）。推定は(推定)と明記。
書式：見出し1行＋箇条書き4–6行。数字は半角、%は小数1桁。2000文字以内に収める。

出力テンプレ：
【{{date_yyyy-mm-dd}} 市況まとめ】
・日本株：日経{{±x.x%}}/TOPIX{{±x.x%}}/グロース{{±x.x%}}。主因：{{要因1・要因2}}。
・個別：{{銘柄}} {{±x.x%}}（理由）。{{銘柄}} {{±x.x%}}（理由）。
・他資産：金{{±x.x%}}、WTI{{±x.x%}}、USD/JPY {{xxx.x}}、米10年{{x.xx%}}。
・イベント：{{イベント1}}、{{イベント2}}、{{イベント3}}。
・明日：{{注目1}}（任意）
"""

prompt = os.environ.get("PROMPT", DEFAULT_PROMPT)

client = OpenAI(api_key=OPENAI_API_KEY)

request_body = {
    "model": "gpt-5",
    "reasoning": {"effort": "medium"},
    "tools": [{"type": "web_search"}],
    "input": [
        {"role": "user", "content": [{"type": "text", "text": prompt}]}
    ],
}

resp = client.responses.create(**request_body)

# Responses API のテキスト抽出（最初の出力を取得）
gpt_text = ""
for item in resp.output:
    if item.type == "message":
        for content_part in item.content:
            if content_part.type == "text":
                gpt_text += content_part.text

# Discord Webhook へ送信（成功は通常 204）
r = requests.post(WEBHOOK_URL, json={"content": gpt_text})
if r.status_code == 204:
    print("送信成功！")
else:
    print(f"送信失敗: {r.status_code} / {r.text}")
