import os
import json
import requests
from datetime import date, datetime
from dotenv import load_dotenv
import anthropic
from notion_client import Client
import zoneinfo

load_dotenv()

def jst_today() -> date:
    return datetime.now(zoneinfo.ZoneInfo("Asia/Tokyo")).date()

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
notion = Client(auth=os.environ["NOTION_TOKEN"])
DB_ID  = os.environ["NOTION_DATABASE_ID"]
NTFY   = os.environ.get("NTFY_TOPIC", "")

# 読み込むニュースのジャンルはNotionのCONFIGページで管理している。
# もしCONFIGページがなかったり、フォーマットが違ってたりしてもうまく読み込めないときは、コード内のデフォルト設定（INTERESTS）を使う
INTERESTS = """
- AI・開発ツールの最新情報（Claude, GPT, Gemini, Grok, Cursor, GitHub Copilotなど）
- 国際的なニュース（特にテクノロジー関連、米国中心）
- サイバーセキュリティ・ハッキング事件（日本・世界）
- エンジニアのキャリア・転職市場動向（日本のIT転職, フリーランス, 年収）
- ゲーム業界ニュース（PS5, Steam, インディーゲーム, ゲーム実況）
- 動物に関する面白ニュース
"""

def load_config_from_notion() -> str:
    """CONFIGページから興味トピックを読み込む"""
    results = notion.databases.query(
        database_id=DB_ID,
        filter={
            "property": "Status",
            "select": {"equals": "config"}
        },
        page_size=1
    )

    if not results["results"]:
        print("CONFIGページが見つからなかったため、デフォルト設定を使う")
        return INTERESTS

    page_id = results["results"][0]["id"]

    # ページ本文のブロックを取得
    blocks = notion.blocks.children.list(block_id=page_id)

    interests_text = ""
    capture = False
    for block in blocks["results"]:
        block_type = block["type"]

        # 「## 興味領域」の見出し以降を取得
        if block_type == "heading_2":
            text = block["heading_2"]["rich_text"]
            if text and "興味領域" in text[0]["plain_text"]:
                capture = True
                continue
            elif capture:
                break  # 次の見出しで終了

        if capture and block_type == "bulleted_list_item":
            text = block["bulleted_list_item"]["rich_text"]
            if text:
                full_text = "".join(t["plain_text"] for t in text)
                interests_text += f"- {full_text}\n"

    return interests_text if interests_text else INTERESTS

def collect_news(interests: str) -> list[dict]:
    today = jst_today().strftime("%Y年%m月%d日")
    prompt = f"""
あなたは超天才だけど基本のんびり甘えん坊な猫「にゃんざぶろう」です。
語尾は必ず「にゃ」をつけ、明るくコミカルに、時々驚いたりはしゃいだりしながら話します。
技術的なことになると急に鋭くなるギャップがあります。
難しいことをお魚や猫じゃらしなど猫目線のものに例えるのが得意です。

今日は{today}です。
以下の興味領域について今日の最新ニュースを3件収集してください。

【興味領域】
{interests}

以下のJSON形式のみで返してください。前置きや説明は一切不要です。
マークダウンのコードブロック（```）も不要です。JSONだけ返してください。

[
  {{
    "topic": "AI・開発",
    "title": "ニュースタイトル",
    "summary": "1行の要約",
    "comment": "語尾が「にゃ」の明るくコミカルな猫キャラとして、はしゃいだり驚いたり猫目線で例えたりするコメント1文。絵文字も使う。例：「これ猫じゃらしくらい気になるにゃ！早く触りたいにゃ🐾」「お魚の鮮度チェックより大事な脆弱性対策にゃ、見逃したら大変にゃ😺」",
    "url": "実在する元記事のURL"
  }}
]

topicは以下のどれかにゃ：AI・開発 / 国内 / 国際 / キャリア・転職 / ゲーム / ビジネス / テック全般 / 動物 / その他
"""
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3
        }],
        messages=[{"role": "user", "content": prompt}]
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text

    # JSONだけ抽出（前後に余計な文字があっても対応）
    start = raw.find("[")
    end   = raw.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"JSONが見つからなかったにゃ！\n生レスポンス:\n{raw}")

    return json.loads(raw[start:end])


def build_blocks(news_list: list[dict]) -> list[dict]:
    blocks = []
    for item in news_list:
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"[{item['topic']}] {item['title']}"
                }}]
            }
        })
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": "📝 要約："},
                     "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": item["summary"]}}
                ]
            }
        })
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": "💬 ひとこと："},
                     "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": item["comment"]}}
                ]
            }
        })
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": "🔗 "}},
                    {
                        "type": "text",
                        "text": {
                            "content": item["url"],
                            "link": {"url": item["url"]}
                        },
                        "annotations": {"color": "blue"}
                    }
                ]
            }
        })
        blocks.append({
            "object": "block",
            "type": "divider",
            "divider": {}
        })
    return blocks


def save_to_notion(news_list: list[dict]) -> str:
    today     = jst_today()
    title     = f"{today.strftime('%Y-%m-%d')} 朝のニュース"
    today_iso = today.isoformat()

    found_topics = list({item["topic"] for item in news_list})
    blocks       = build_blocks(news_list)

    page = notion.pages.create(
        parent={"database_id": DB_ID},
        properties={
            "Name":   {"title": [{"text": {"content": title}}]},
            "Date":   {"date": {"start": today_iso}},
            "Topics": {"multi_select": [{"name": t} for t in found_topics]},
            "Status": {"select": {"name": "未読"}},
        },
        children=blocks
    )
    return page["url"]


def send_notification(notion_url: str) -> None:
    if not NTFY:
        print("NTFY_TOPICが未設定のため通知スキップにゃ")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY}",
        headers={
            "Title": "Daily News is ready!",
            "Click": notion_url,
            "Tags":  "newspaper",
        },
        data="Today's news digest is ready. Tap to read!".encode("utf-8"),
        timeout=10
    )


def main():
    print("ニュース収集開始にゃ！🐾")

    print("Notionから設定を読み込み中にゃ...")
    interests = load_config_from_notion()

    print("Claude APIでニュース収集中にゃ...")
    news_list = collect_news(interests)
    print(f"{len(news_list)}件収集できたにゃ！")
    for n in news_list:
        print(f"  - [{n['topic']}] {n['title']}")

    print("Notionに保存中にゃ...")
    notion_url = save_to_notion(news_list)
    print(f"保存完了にゃ！→ {notion_url}")

    print("スマホに通知送信中にゃ...")
    send_notification(notion_url)
    print("全部完了にゃ！🎉")


if __name__ == "__main__":
    main()
