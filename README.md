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

## デプロイとフォルダwatchの開始

- アクセストークン取得したい場合は/auth/startを叩く

```bash
user/gooogle-drive-watcer> gcloud run deploy aob-drive-watch --source . --region xxx --project xxx  # デプロイ
user/gooogle-drive-watcer> irm -Uri https://xxx/register-watch -Method POST # Drive Watch開始!
```

