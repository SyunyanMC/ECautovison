#!/usr/bin/env python3
"""
EC New Item Notifier

楽天市場商品検索APIで新着商品をチェックし、初めて見た商品だけを
Discord Webhookへ通知する。既出チェックは DATABASE_URL の
PostgreSQL (Neon等) に保存する。DATABASE_URL が無い場合は
ローカルの SQLite ファイルに保存する（動作確認用）。

必須の環境変数 (GitHub Secrets):
  RAKUTEN_APP_ID        楽天アプリID
  DISCORD_WEBHOOK_URL   通知先DiscordのWebhook URL
  DATABASE_URL          PostgreSQL接続URL

任意の環境変数:
  RAKUTEN_ACCESS_KEY    新方式の楽天API用アクセスキー (pk_...)
                        指定がある場合は openapi.rakuten.co.jp を使う
  WATCH_KEYWORD         監視キーワード   (GitHub Variables推奨)
  WATCH_SHOP_CODE       監視ショップコード (例: rakuten24)
  WATCH_GENRE_ID        監視ジャンルID
    ※ WATCH_KEYWORD / WATCH_SHOP_CODE / WATCH_GENRE_ID の
       いずれか1つは必須
  WATCH_PAGES           取得ページ数 (1ページ30件, 既定: 1)
  MAX_NOTIFY_PER_RUN    1回の実行で通知する最大件数 (既定: 10)

初回実行時は、その時点の商品を「既出」として登録するだけで
通知は送らない（通知スパム防止）。2回目以降、新しい商品だけ通知する。
"""

from __future__ import annotations

import os
import sys
import time
import json
import sqlite3
import datetime
from dataclasses import dataclass

import requests

OLD_API_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
NEW_API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

REQUEST_TIMEOUT = 20
DISCORD_MAX_EMBEDS = 10


def log(msg: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {msg}", flush=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---------------------------------------------------------------- storage

@dataclass
class Item:
    code: str
    name: str
    url: str
    price: int | None
    shop_name: str
    image_url: str


class Store:
    """seen_items の保存先。PostgreSQL か SQLite のどちらかを使う。"""

    def __init__(self, database_url: str):
        self.use_pg = database_url.startswith(("postgres://", "postgresql://"))
        if self.use_pg:
            import psycopg  # lazy import: SQLite利用時は不要

            self.conn = psycopg.connect(database_url, autocommit=True)
            self.ph = "%s"
        else:
            path = database_url.replace("sqlite:///", "") if database_url else "seen_items.db"
            self.conn = sqlite3.connect(path)
            self.ph = "?"
        self._create_table()

    def _create_table(self) -> None:
        ts_type = "timestamptz DEFAULT now()" if self.use_pg else "text DEFAULT (datetime('now'))"
        cur = self.conn.cursor()
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_code  text PRIMARY KEY,
                item_name  text,
                item_url   text,
                item_price integer,
                shop_name  text,
                first_seen {ts_type}
            )
            """
        )
        if not self.use_pg:
            self.conn.commit()

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM seen_items")
        row = cur.fetchone()
        return int(row[0])

    def known_codes(self, codes: list[str]) -> set[str]:
        if not codes:
            return set()
        placeholders = ",".join([self.ph] * len(codes))
        cur = self.conn.cursor()
        cur.execute(f"SELECT item_code FROM seen_items WHERE item_code IN ({placeholders})", codes)
        return {r[0] for r in cur.fetchall()}

    def save(self, items: list[Item]) -> None:
        if not items:
            return
        sql = (
            f"INSERT INTO seen_items (item_code, item_name, item_url, item_price, shop_name) "
            f"VALUES ({self.ph},{self.ph},{self.ph},{self.ph},{self.ph}) "
            + ("ON CONFLICT (item_code) DO NOTHING" if self.use_pg else "ON CONFLICT(item_code) DO NOTHING")
        )
        cur = self.conn.cursor()
        cur.executemany(sql, [(i.code, i.name, i.url, i.price, i.shop_name) for i in items])
        if not self.use_pg:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------- rakuten

def fetch_items(app_id: str, access_key: str) -> list[Item]:
    keyword = env("WATCH_KEYWORD")
    shop_code = env("WATCH_SHOP_CODE")
    genre_id = env("WATCH_GENRE_ID")
    if not (keyword or shop_code or genre_id):
        raise SystemExit(
            "監視対象が未設定です。GitHub の Settings > Secrets and variables > "
            "Actions > Variables に WATCH_KEYWORD / WATCH_SHOP_CODE / "
            "WATCH_GENRE_ID のいずれかを設定してください。"
        )

    pages = max(1, int(env("WATCH_PAGES", "1") or "1"))
    api_url = NEW_API_URL if access_key else OLD_API_URL

    items: list[Item] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        params: dict[str, str | int] = {
            "applicationId": app_id,
            "format": "json",
            "formatVersion": 2,
            "hits": 30,
            "page": page,
            "sort": "-updateTimestamp",
            "availability": 1,
        }
        if access_key:
            params["accessKey"] = access_key
        if keyword:
            params["keyword"] = keyword
        if shop_code:
            params["shopCode"] = shop_code
        if genre_id:
            params["genreId"] = genre_id

        data = _get_json(api_url, params)
        raw_items = data.get("Items", [])
        for raw in raw_items:
            # formatVersion=2 はフラット、1 は {"Item": {...}} 形式。両対応。
            it = raw.get("Item", raw) if isinstance(raw, dict) else {}
            code = str(it.get("itemCode") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            images = it.get("mediumImageUrls") or it.get("smallImageUrls") or []
            image_url = ""
            if images:
                first = images[0]
                image_url = first.get("imageUrl", "") if isinstance(first, dict) else str(first)
            price = it.get("itemPrice")
            items.append(
                Item(
                    code=code,
                    name=str(it.get("itemName") or code)[:200],
                    url=str(it.get("itemUrl") or ""),
                    price=int(price) if isinstance(price, (int, float)) else None,
                    shop_name=str(it.get("shopName") or ""),
                    image_url=image_url.replace("?_ex=128x128", "?_ex=256x256"),
                )
            )
        if page < pages:
            time.sleep(1.1)  # 楽天APIの目安: 1リクエスト/秒
    log(f"楽天APIから {len(items)} 件取得")
    return items


def _get_json(url: str, params: dict) -> dict:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            res = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if res.status_code == 200:
                return res.json()
            if res.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            detail = ""
            try:
                detail = str(res.json())[:300]
            except Exception:
                detail = res.text[:300]
            if res.status_code in (400, 401, 403):
                raise SystemExit(
                    f"楽天APIが認証エラーを返しました (HTTP {res.status_code}): {detail}\n"
                    "新方式のアプリIDの場合は RAKUTEN_ACCESS_KEY (pk_...) も "
                    "Secrets に設定が必要です。"
                )
            raise SystemExit(f"楽天APIエラー (HTTP {res.status_code}): {detail}")
        except requests.RequestException as e:  # ネットワーク系はリトライ
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"楽天APIへの接続に失敗しました: {last_err}")


# ---------------------------------------------------------------- discord

def notify_discord(webhook_url: str, items: list[Item]) -> None:
    """新着を Discord へ。10件ずつ embeds で送る。"""
    max_notify = max(1, int(env("MAX_NOTIFY_PER_RUN", "10") or "10"))
    to_send = items[:max_notify]
    embeds = []
    for it in to_send:
        embed: dict = {
            "title": ("🆕 " + it.name)[:256],
            "url": it.url,
            "color": 0xBF0000,
            "fields": [],
        }
        if it.price is not None:
            embed["fields"].append({"name": "価格", "value": f"¥{it.price:,}", "inline": True})
        if it.shop_name:
            embed["fields"].append({"name": "ショップ", "value": it.shop_name[:1024], "inline": True})
        if it.image_url:
            embed["thumbnail"] = {"url": it.image_url}
        embeds.append(embed)

    content = f"**楽天市場の新着商品 {len(items)} 件**"
    if len(items) > max_notify:
        content += f"（通知は最初の {max_notify} 件まで、残りは登録のみ）"
    _post_discord(webhook_url, {"content": content, "embeds": embeds})


def notify_baseline(webhook_url: str, count: int) -> None:
    _post_discord(
        webhook_url,
        {
            "content": (
                f"✅ EC New Item Notifier の初回登録が完了しました。"
                f"現在の商品 {count} 件を既出として登録しました。"
                f"次回から新着だけを通知します。"
            )
        },
    )


def _post_discord(webhook_url: str, payload: dict) -> None:
    for attempt in range(3):
        res = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        if res.status_code in (200, 204):
            return
        if res.status_code == 429 and attempt < 2:
            try:
                wait = float(res.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            time.sleep(min(wait, 10))
            continue
        raise SystemExit(f"Discord通知に失敗しました (HTTP {res.status_code}): {res.text[:300]}")


# ---------------------------------------------------------------- main

def main() -> int:
    app_id = env("RAKUTEN_APP_ID")
    access_key = env("RAKUTEN_ACCESS_KEY")
    webhook_url = env("DISCORD_WEBHOOK_URL")
    database_url = env("DATABASE_URL")

    missing = [
        name
        for name, val in [
            ("RAKUTEN_APP_ID", app_id),
            ("DISCORD_WEBHOOK_URL", webhook_url),
        ]
        if not val
    ]
    if missing:
        raise SystemExit(f"必須の Secrets が未設定です: {', '.join(missing)}")
    if not database_url:
        log("警告: DATABASE_URL が未設定のためローカルSQLiteに保存します（GitHub Actionsでは毎回初回扱いになります）")

    items = fetch_items(app_id, access_key)
    if not items:
        log("対象商品が0件でした。監視条件を見直してください。")
        return 0

    store = Store(database_url)
    try:
        is_first_run = store.count() == 0
        if is_first_run:
            store.save(items)
            log(f"初回実行: {len(items)} 件を既出として登録（通知なし）")
            notify_baseline(webhook_url, len(items))
            return 0

        known = store.known_codes([i.code for i in items])
        new_items = [i for i in items if i.code not in known]
        if not new_items:
            log("新着はありませんでした")
            return 0

        log(f"新着 {len(new_items)} 件を通知します")
        notify_discord(webhook_url, new_items)  # 通知が成功してから保存する
        store.save(new_items)
        log("新着を保存しました")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"エラー: {e.code}", file=sys.stderr)
        raise
