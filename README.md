# サークル管理Bot 総合導入・運用手順書

このBotは、Discord上で **「出欠管理」** と **「部室・施設の利用状況」** を自動化・管理するためのマルチサーバー対応システムです。
クラウド（Render + Turso DB）およびローカル環境での24時間安定稼働に対応しています。

---

## 1. 事前準備

1. **Discord Developer Portal での Bot 設定**
   - [Discord Developer Portal](https://discord.com/developers/applications) にアクセスし、Botの **Token（トークン）** を取得します。
   - 「Bot」タブの **Privileged Gateway Intents** で以下をすべて **ON** にしてください：
     - ✅ **Presence Intent**
     - ✅ **Server Members Intent**
     - ✅ **Message Content Intent**
2. **サーバーへの招待**
   - 「OAuth2」->「URL Generator」にて、`bot` と `applications.commands` を選択。
   - `Administrator`（管理者権限）を付与して生成されたURLからBotをサーバーに招待します。

---

## 2. 初期設定

1. **ライブラリのインストール**
   ```bash
   pip install -r requirements.txt
   ```

2. **`.env` ファイルの設定**
   `.env.example` をコピーして `.env` を作成し、必要な環境変数を入力します：

   ```env
   # Discord Bot トークン (必須)
   DISCORD_TOKEN=your_bot_token_here

   # Turso クラウドDB設定 (Render運用時は必須 / ローカル時は空欄でSQLite自動使用)
   TURSO_DATABASE_URL=https://your-db-name.turso.io
   TURSO_AUTH_TOKEN=your_turso_auth_token

   # スラッシュコマンド初回同期フラグ (通常は false, コマンド追加時のみ true または !sync)
   SYNC_COMMANDS=false
   ```

---

## 3. ディレクトリ構造

```text
ディスコードBot/
├── config.py              # 設定・環境変数の一括管理
├── database.py            # Turso / SQLite 自動判別データベース層
├── circuit_breaker.py     # 過剰リクエスト事前遮断 & 自己防衛システム
├── web_server.py          # Render スリープ防止用 Web サーバー (ポート開放)
├── ui/                    # ボタン・セレクト・モーダル等のUI部品
│   ├── __init__.py
│   ├── attendance.py      # 出欠パネル View
│   ├── room_status.py     # 部室・施設状況 View / Modal
│   └── admin_panel.py     # 管理パネル View & イベント作成Modal
├── bot.py                 # Bot 本体クラス & コマンド定義
└── main.py                # 超軽量エントリーポイント (起動スクリプト)
```

---

## 4. コマンド一覧

### 📌 スラッシュコマンド（一般設定）
* **`/setup`** (管理者専用):
  * 各種パネルを設置するチャンネル（管理、出欠、部室状況）を設定します。

### 📌 管理者専用プレフィックスコマンド
* **`!sync`**: スラッシュコマンドを手動で Discord 側に即時同期します。
* **`!status`**: Bot の稼働状態、サーキットブレーカーの防衛状況、直近通信頻度を表示します。
* **`!maintenance on` / `!maintenance off`**: メンテナンスモードの有効化・解除（一般ユーザーの操作受付を一時停止）。
* **`!shutdown`** (または `!stop`): Discord 上から Bot を安全に切断・シャットダウンします。

---

## 5. 安全機能・自己防衛システム（Circuit Breaker）

* **レートリミット事前遮断**: 
  短時間（5秒間）に 10回以上 の操作を検知した場合、Cloudflare / Discord による IP ブロックを未然に防ぐため、Discord API に送る手前で自動的にリクエストを遮断し、60秒間クールダウンします。
* **Render クラッシュループ防止**: 
  起動直後に別スレッドでポート（`PORT`）を開放し、Render のヘルスチェックを瞬時にパスさせます。
