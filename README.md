# ECautovison 修正ファイル一式

`SyunyanMC/ECautovison` の失敗修正用ファイルです。

## 入れるファイル

| ファイル | 置き場所 |
|---|---|
| `main.py` | リポジトリのルート |
| `requirements.txt` | リポジトリのルート |
| `notify.yml` | `.github/workflows/notify.yml` と置き換え |

`requirements.txt` が入ることで、`setup-python` の `cache: pip` が
正常に動くようになり、4秒で失敗しなくなります。

## push前に必要な設定 (GitHub)

リポジトリの **Settings > Secrets and variables > Actions**:

### Secrets（登録済みのはず）
- `RAKUTEN_APP_ID`
- `DISCORD_WEBHOOK_URL`
- `DATABASE_URL`
- `RAKUTEN_ACCESS_KEY`（任意。新方式の楽天アプリIDの場合のみ必要）

### Variables（今回追加で必要）
監視対象をどれか1つ以上:
- `WATCH_KEYWORD` … 例: 商品名キーワード
- `WATCH_SHOP_CODE` … 例: ショップコード
- `WATCH_GENRE_ID` … ジャンルID

## 動作

- 15分ごとに楽天市場を検索し、DBに無い商品だけDiscordへ通知
- 初回だけは現在の商品を登録するだけで通知しない
- 既出の保存先は `DATABASE_URL` の PostgreSQL (`seen_items` テーブル、自動作成)
