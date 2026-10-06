# Google Drive Watcher

- Google Driveの特定のディレクトリの変更をGoogle Chatに通知するリポジトリ

## 使い方

1. GCPを始める
1. 有効なAPIサービスとしてgoogle chat apiとgoogle drive apiを追加
1. それぞれのAPIのスコープも良い感じにする(read, write)
1. Google Chat APIの構成で適当に欄を埋めるのを忘れずに(これ忘れると投稿が出来ません・・)
1. Google Driveの監視したいディレクトリのIDとGoogle Chatの対象のスペースIDを.envに記載！
1. OAthでそれぞれのAPI叩くんでOathクライアントの追加も忘れずに
1. あとデータベースの名前を.envで指定しておくこと。FireStoreに更新があったファイルとか保存します
1. cron実行でフォルダの変更点をgoogle chatに通知するのでcloude scheduleも良い感じに設定しておくこと。変更点の蓄積はwebhookなのだけれども
1. cronで叩くcurlのヘッダにtoken追加するのを忘れずに

## 変更検知のスコープ設定

- 「どの変更を通知するか」は環境変数 `CHANGE_SCOPE` で切り替えられます
- プリセットは `main.py` の `CHANGE_SCOPE_PRESETS`（data駆動）に定義。行を足せばパターンを追加できます
- 未指定時のデフォルトは `added_only`（新規追加のみ）

| CHANGE_SCOPE | 通知対象 |
| --- | --- |
| `added_only` | 新規追加のみ（デフォルト） |
| `added_and_modified` | 追加 + 既存ファイルの更新 |
| `added_modified_trashed` | 追加 + 更新 + ゴミ箱移動 |
| `all` | 追加 + 更新 + ゴミ箱 + 削除 |

- 変更種別の内訳
  - `added` … 新規作成（`createdTime` と `modifiedTime` がほぼ同時刻かで判定）
  - `modified` … 既存ファイルの更新
  - `trashed` … ゴミ箱へ移動
  - `removed` … 削除 / アクセス喪失
- 注意: `removed` は Drive から file 情報も親フォルダも返ってこないため、`TARGET_FOLDER_ID` 配下かどうかの判定ができません（`all` 指定時は Drive 全体の削除が対象になります）

```bash
# .env 例
CHANGE_SCOPE=added_only
```

## デプロイとフォルダwatchの開始

- アクセストークン取得したい場合は/auth/startを叩く

```bash
user/gooogle-drive-watcer> gcloud run deploy aob-drive-watch --source . --region xxx --project xxx  # デプロイ
user/gooogle-drive-watcer> irm -Uri https://xxx/register-watch -Method POST # Drive Watch開始!
```

