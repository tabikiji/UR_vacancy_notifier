# UR空室通知（ur-vacancy-notifier）

UR賃貸の公式サイトを1時間ごとに確認し、条件に合う**新しい空室**が出たら Discord に通知します。

## 仕組み（2段階）

| フェーズ | スクリプト | 実行 | 内容 |
|---|---|---|---|
| A. 候補団地の洗い出し | `discover.py` | 初回・条件変更時に手動 | 通勤条件（例: 通勤先A駅60分・通勤先B駅40分）＋駅徒歩15分で団地を検索 → `candidates.yaml` |
| B. 空室監視 | `monitor.py` | GCP で毎時 | 通勤条件の検索1回で各団地の空室数を取得 → `watchlist.yaml` の団地のうち空室がある所だけ部屋一覧を取得 → 部屋条件（2階以上など）で絞り込み → 前回との差分を Discord 通知 |

- データ源: UR サイトが内部で使っている JSON API（`chintai.r6.ur-net.go.jp/chintai/api/...`）と、通勤時間データ（`/chintai/common/xml/cost-time/...`）
- 1回の実行のリクエスト数は「検索 約5回 ＋ 空室のある監視団地の数」程度。リクエスト間隔は1.5秒です
- 前回の結果は GCS（`ur-vacancy-state.json`）に保存しています。同じ部屋の通知は1回だけです

## 条件と設定（`config.yaml`）

| 希望 | 対応 |
|---|---|
| 通勤先Aまで通勤1時間以内 | `commute` 通勤先A `max_min: 60`（駅→駅の電車時間。徒歩込みにしたいなら 45 などに下げる） |
| 通勤先Bまで電車40分以内 | `commute` 通勤先B `max_min: 40` |
| 駅徒歩15分以内 | `walk_max: 15`（バスを使うルートは除外。棟によって差がある場合は最短の棟で判定） |
| 2階以上 | `filters.floor_min: 2` |
| 寝室6畳以上 | ⚠️ API に畳数の情報がないため自動判定はしません。代わりに**通知に間取り図を大きく表示**します |

家賃上限・間取り・面積も `filters` で設定できます。

### 家賃・間取りの目視確認用の列

`discover.py` は、各団地のページ（管理物件全体の情報）から次の列を `candidates.yaml` に付けます。空室がない団地でも取得できます。`watchlist.yaml` から高い団地を目視で削るときに使います（`monitor.py` は使いません）。

| 列 | 例 |
|---|---|
| `rent_range` | `93,300～149,200円（共益費 6,200円）` |
| `madori_range` | `1DK・2DK / 45～55㎡` |
| `rent_min` / `rent_max` | `93300` / `149200`（並べ替え・除外用） |

※家賃は定期借家等の割引適用後の場合があり、下限は割引込みの可能性があります。高い団地を外す判断は `rent_max` が安全です。

すでにある `watchlist.yaml` には、検索をやり直さずに列だけ追記できます（取得済みの団地はスキップ）。

```bash
python discover.py --enrich watchlist.yaml
```

## 使い方

### 初期設定

設定ファイルは個人の条件を含むため、リポジトリには含めていません。サンプルをコピーして使います。

```bash
cp config.example.yaml config.yaml        # 通勤先・家賃などを自分の条件に書き換える
python discover.py                         # candidates.yaml を生成
# 不要な団地を削除して watchlist.yaml として保存（watchlist.example.yaml は形式の見本）
```

デプロイは手動です。変更をGitHubにpushしたあと、手元で `./deploy.sh` を実行してGCPに反映します。

### 実行

```bash
pip install -r requirements.txt

# A. 候補団地の洗い出し（UR サイトに届く環境で実行）
python discover.py              # → candidates.yaml
#   要らない団地を削除して watchlist.yaml として保存
#   （watchlist.example.yaml は形式の見本で、2団地のみ）

# B. ローカルで試す（DRY_RUN=1 なら通知せずに内容だけ表示）
DRY_RUN=1 python monitor.py

# GCP にデプロイ（Cloud Run Jobs + Cloud Scheduler 毎時）
PROJECT_ID=your-project DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx ./deploy.sh
gcloud run jobs execute ur-vacancy-notifier --region asia-northeast1 --wait   # 手動で1回
```

`config.yaml` や `watchlist.yaml` を変更した場合は、`./deploy.sh` をもう一度実行してください（`DISCORD_WEBHOOK_URL` は省略可）。

## 駅コードの調べ方

UR サイトの「通勤・通学時間から探す」で駅名を入力し、候補リストに出る駅の `data-value` が駅コードです。
例: 東京 = `0000`（実際のコードはUR サイトで確認）

## 注意

- UR サイトの非公開 API を使っています。サイトの仕様が変わると動かなくなる可能性があります。全団地の取得に失敗した場合は Discord に⚠️を通知します
- 通勤時間は UR サイトのデータ（日中の平均値、待ち時間は含まない）に基づきます
- アクセスは1時間に1回、最小限のリクエストに抑えています
