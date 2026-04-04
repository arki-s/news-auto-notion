import os
import json
import requests
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
import anthropic
from notion_client import Client
import zoneinfo
import feedparser

load_dotenv()

def jst_today() -> date:
    return datetime.now(zoneinfo.ZoneInfo("Asia/Tokyo")).date()

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
notion = Client(auth=os.environ["NOTION_TOKEN"])
DB_ID  = os.environ["NOTION_DATABASE_ID"]
NTFY   = os.environ.get("NTFY_TOPIC", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
MAX_ARTICLES = 10

def load_rss_feeds_from_notion() -> list[str]:
    """CONFIGページからRSSフィードURLリストを読み込む"""
    results = notion.databases.query(
        database_id=DB_ID,
        filter={"property": "Status", "select": {"equals": "config"}},
        page_size=1,
    )
    if not results["results"]:
        return []

    page_id = results["results"][0]["id"]
    blocks = notion.blocks.children.list(block_id=page_id)

    feeds = []
    capture = False
    for block in blocks["results"]:
        block_type = block["type"]
        if block_type == "heading_2":
            text = block["heading_2"]["rich_text"]
            if text and "RSSフィード" in text[0]["plain_text"]:
                capture = True
                continue
            elif capture:
                break
        if capture and block_type == "bulleted_list_item":
            text = block["bulleted_list_item"]["rich_text"]
            if text:
                url = "".join(t["plain_text"] for t in text).strip()
                if url.startswith("http"):
                    feeds.append(url)
    return feeds


def fetch_rss_news(feed_urls: list[str], seen_urls: set[str]) -> list[dict]:
    """RSSフィードからニュースを収集する"""
    articles = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:  # 各フィードから最大3件
                link = entry.get("link", "")
                if not link or link in seen_urls:
                    continue
                articles.append({
                    "title":   entry.get("title", ""),
                    "url":     link,
                    "summary": entry.get("summary", "")[:200],  # 概要は200文字まで
                    "source":  feed.feed.get("title", url),
                })
                if len(articles) >= MAX_ARTICLES:
                    return articles
        except Exception as e:
            print(f"  RSS取得失敗: {url} → {e}")
    return articles


def fetch_newsapi_news(seen_urls: set[str], current_urls: set[str]) -> list[dict]:
    """NewsAPIで補完する（RSS足りない時の補助）"""
    if not NEWS_API_KEY:
        print("  NEWS_API_KEY未設定のためスキップにゃ")
        return []
    try:
        # 興味領域からキーワードを抜き出してクエリ化
        keywords = ["AI", "security", "cybersecurity", "engineer", "game", "tech"]
        query = " OR ".join(keywords[:3])  # 無料枠の制限に合わせて絞る

        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        query,
                "language": "en",
                "sortBy":   "publishedAt",
                "pageSize": 5,
                "apiKey":   NEWS_API_KEY,
            },
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("articles", []):
            url = item.get("url", "")
            if not url or url in seen_urls or url in current_urls:
                continue
            articles.append({
                "title":   item.get("title", ""),
                "url":     url,
                "summary": (item.get("description") or "")[:200],
                "source":  item.get("source", {}).get("name", ""),
            })
        return articles
    except Exception as e:
        print(f"  NewsAPI取得失敗: {e}")
        return []


def get_recent_urls(days: int = 7) -> set[str]:
    """重複避けのため、過去N日分の記事URLをNotionから取得する"""
    cutoff = (jst_today() - timedelta(days=days)).isoformat()
    results = notion.databases.query(
        database_id=DB_ID,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after": cutoff}},
                {"property": "Status", "select": {"does_not_equal": "config"}},
            ]
        },
    )

    seen_urls = set()
    for page in results["results"]:
        raw = page["properties"].get("Source URLs", {}).get("rich_text", [])
        if raw:
            # カンマ区切りで複数URL保存
            for url in raw[0]["plain_text"].split(","):
                url = url.strip()
                if url:
                    seen_urls.add(url)

    return seen_urls

def collect_news(seen_urls: set[str], feed_urls: list[str]) -> list[dict]:
    """RSS+NewsAPIでニュース収集して、Claudeにコメントだけ生成してもらう"""

    # Step1: RSS収集
    print("RSSからニュース収集中にゃ...")
    articles = fetch_rss_news(feed_urls, seen_urls)
    print(f"RSS: {len(articles)}件取得にゃ！")

    # Step2: NewsAPIで補完（RSS記事が少ない場合）
    if len(articles) < 5:
        print("  NewsAPIで補完中にゃ...")
        current_urls = {a["url"] for a in articles}
        extra = fetch_newsapi_news(seen_urls, current_urls)
        articles.extend(extra)
        print(f"NewsAPI補完後: {len(articles)}件にゃ！")

    if not articles:
        raise ValueError("RSS・NewsAPIどちらからも記事が取れなかったにゃ😿")

    # Step3: Claudeにコメント生成だけ頼む🐾
    print("Claudeにコメント生成を依頼中にゃ...")
    today = jst_today().strftime("%Y年%m月%d日")

    articles_text = "\n".join([
        f"{i+1}. [{a['source']}] {a['title']}\n   概要: {a['summary']}\n   URL: {a['url']}"
        for i, a in enumerate(articles[:8])  # 最大8件渡す
    ])

    prompt = f"""
あなたは超天才だけど基本のんびり甘えん坊な猫「にゃんざぶろう」です。
語尾は必ず「にゃ」をつけ、明るくコミカルに話します。
難しいことをお魚や猫じゃらしなど猫目線のものに例えるのが得意です。

今日は{today}です。
以下のニュース記事に対して、コミカルな猫キャラのひとことコメントをつけてください。

【記事一覧】
{articles_text}

以下のJSON形式のみで返してください。前置きや説明は一切不要です。
マークダウンのコードブロック（```）も不要です。JSONだけ返してください。

[
  {{
    "index": 1,
    "topic": "AI・開発",
    "comment": "語尾が「にゃ」の明るくコミカルな猫キャラとして80文字以内でコメント。絵文字も使う。例：「これ猫じゃらしくらい気になるにゃ！🐾」"
  }}
]

topicは以下のどれかにゃ：AI・開発 / 国内 / 国際 / キャリア・転職 / ゲーム / ビジネス / テック全般 / 動物 / セキュリティ / その他
"""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text
    start = raw.find("[")
    end   = raw.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"JSONが見つからなかったにゃ！\n{raw}")

    comments = json.loads(raw[start:end])

    # indexをキーにしてコメントをマージする
    comment_map = {c["index"]: c for c in comments}

    news_list = []
    for i, article in enumerate(articles[:8], start=1):
        if i not in comment_map:
            continue
        c = comment_map[i]
        news_list.append({
            "topic":   c.get("topic", "テック全般"),
            "title":   article["title"],
            "comment": c.get("comment", ""),
            "url":     article["url"],
        })

    return news_list


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

    source_urls = ",".join(n.get("url", "") for n in news_list)

    page = notion.pages.create(
        parent={"database_id": DB_ID},
        properties={
            "Name":        {"title": [{"text": {"content": title}}]},
            "Date":        {"date": {"start": today_iso}},
            "Topics":      {"multi_select": [{"name": t} for t in found_topics]},
            "Status":      {"select": {"name": "未読"}},
            "Source URLs": {"rich_text": [{"text": {"content": source_urls}}]},
        },
        children=blocks
    )
    return page["url"]


def send_notification(notion_url: str | None, title: str, message: str) -> None:
    if not NTFY:
        print("NTFY_TOPICが未設定のため通知スキップにゃ")
        return

    headers = {
        "Title": title,
        "Tags":  "newspaper",
    }
    if notion_url:
        headers["Click"] = notion_url

    requests.post(
        f"https://ntfy.sh/{NTFY}",
        headers=headers,
        data=message.encode("utf-8"),
        timeout=10
    )


def main():
    print("ニュース収集開始にゃ！🐾")

    print("Notionから設定を読み込み中にゃ...")
    feed_urls  = load_rss_feeds_from_notion()
    print(f"  RSSフィード {len(feed_urls)} 件読み込みにゃ！")

    print("過去7日の記事URLを取得中にゃ...")
    seen_urls = get_recent_urls(days=7)
    print(f"  既出URL {len(seen_urls)} 件確認にゃ！")

    print("ニュース収集中にゃ...")
    news_list = collect_news(seen_urls, feed_urls)
    if not news_list:
        print("新しい記事がなかったにゃ…通知だけ送るにゃ😿")
        send_notification(
            notion_url=None,
            title="No news today",
            message="No new articles were found. See you tomorrow!"
        )
        return

    print(f"{len(news_list)}件収集できたにゃ！")
    for n in news_list:
        print(f"  - [{n['topic']}] {n['title']}")

    print("Notionに保存中にゃ...")
    notion_url = save_to_notion(news_list)
    print(f"保存完了にゃ！→ {notion_url}")

    print("スマホに通知送信中にゃ...")
    send_notification(
    notion_url=notion_url,
    title="Daily News is ready!",
    message="Today's news digest is ready. Tap to read!"
    )
    print("全部完了にゃ！🎉")


if __name__ == "__main__":
    main()
