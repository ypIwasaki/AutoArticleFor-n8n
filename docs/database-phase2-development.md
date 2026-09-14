# 専用DB基盤の開発・起動・復元手順（フェーズ2）

フェーズ2は空の専用DBと基盤の検証までを対象とする。既存Data Tables・ファイルの読取元と書込先はlegacyのまま。実データ取込、継続同期、全件内容比較、互換出力、運用経路の切替は後続フェーズで実装・検証する。

## 実行環境

WSL UbuntuでPython 3.8.10 / SQLite 3.31.1を検証した。Python標準ライブラリだけを利用し、pipパッケージは不要。SQLiteにはJSON関数とWALの利用が必要。接続は外部キーON・待機上限10秒、書込接続はWAL・synchronous=FULL、読取接続はmode=ro・query_only=ONを設定する。書込は明示トランザクション。

以下はプロジェクトルートで実行する。外部の既存DBを指定しない。

```sh
python3 scripts/autoarticle_db.py init
python3 scripts/autoarticle_db.py migrate
python3 scripts/autoarticle_db.py status
python3 scripts/autoarticle_db.py conflicts
```

既定の作成先は `data/autoarticle.sqlite`。`init`と`migrate`は共通の連番マイグレーションを使用し、適用済み版の再実行は変更しない。適用済みSQLのチェックサムが変わっていれば失敗する。変更は新しい `database/migrations/NNN_name.sql` に追加する。失敗時は今回のスキーマ更新全体をロールバックする。`database/schema.sql` はスキーマの参照用。初期化にはCLIを使う。

## 設定

| 環境変数 | 設定 |
| --- | --- |
| AUTOARTICLE_DATABASE_PATH | 専用DBパス。省略時はプロジェクトのdata/autoarticle.sqlite。CLIの--databaseはこの値より優先 |
| AUTOARTICLE_DATA_SOURCE | legacy / project-db。フェーズ2ではlegacyを維持。既存アプリの参照先を自動変更する設定ではない |
| AUTOARTICLE_DB_SERVICE_URL | http://127.0.0.1:8766が既定。127.0.0.1のHTTP originのみ。ポートは1～65535 |
| AUTOARTICLE_DB_SERVICE_TOKEN | 空白なし32文字以上の認証値。環境変数から読み、Gitに保存しない |

`.env`の自動読み込みは行わない。必要な環境変数を起動するシェルに設定する。認証値を引数・URL・ログに入れない。

## ローカルサービスの起動と停止

空DB初期化後、同じシェルで実行する。次の生成値はシェル変数に入り、画面には表示されない。同じ認証値を利用するクライアントも環境変数から取得する。

```sh
export AUTOARTICLE_DATA_SOURCE=legacy
export AUTOARTICLE_DB_SERVICE_URL=http://127.0.0.1:8766
export AUTOARTICLE_DB_SERVICE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 scripts/autoarticle_db_service.py
```

待受は常に127.0.0.1。DB未初期化、認証値不足、不正なURL・データソース指定では起動しない。全APIが `Authorization: Bearer <環境変数の値>` を要求する。サービスの既定ログは待受アドレスだけで、要求本文や認証値を記録しない。

前面起動したシェルでCtrl+Cを押して停止する。フェーズ2では既存n8n起動スクリプトや自動起動設定へ組み込まない。試験用に起動したサービスは試験終了時に停止する。

## APIの範囲

| メソッド・パス | 用途 |
| --- | --- |
| GET /health | DBに接続できることを含む死活確認 |
| GET /v1/status | 適用スキーマ・機能別切替状態・未解決競合数 |
| GET /v1/articles/{id} | 記事・識別子・本文版・取得試行・レビューの取得 |
| POST /v1/articles | 記事・識別子・収集実行・出現履歴 |
| POST /v1/contents | 本文内容・本文版・取得試行 |
| POST /v1/reviews | レビュー・工程状態・事実・人物・根拠・要約 |
| POST /v1/talent-proposals | 未承認人材・別名・提案または保留の関係 |
| POST /v1/classifications | 分類・副カテゴリ |
| POST /v1/feedback | 記事評価 |

POSTのトップレベルは `operationId` と `records` のみ。recordsは各業務で許可されたエンティティ名と行配列。SQLや物理テーブル名を受け取るAPIはない。要求はJSON、上限1MiB、処理IDは先頭英数字・全体8～128文字の英数字と `._:-` のみ。保存対象は1～1000行。

現段階の書込APIは新規レコードの原子的保存を提供する。同じ処理ID・同じ内容の再送は保存済み応答を返し、同じIDで内容が異なる場合は409。更新・版の置換・継続同期を運用へ接続する検証は後続フェーズ。人材提案APIは承認・検索有効化を受け付けない。

成功応答はsuccessCount / updatedCount / existingCount / heldCount / operationId。認証不正は401、入力不正は400、サイズ超過は413、DB障害は503。一部成功は返さず、DB障害時にlegacyへ自動フォールバックしない。

## バックアップと復元確認

日付付きの新しいパスを指定する。既存のバックアップや復元先は上書きしない。

```sh
python3 scripts/autoarticle_db.py backup --output data/backups/2026-09-14-empty.sqlite
python3 scripts/autoarticle_db.py restore-check --snapshot data/backups/2026-09-14-empty.sqlite --output data/backups/2026-09-14-restore-test.sqlite
```

元・宛先を共通接続で開き、両方のforeign_keys=1を確認してSQLite backup APIでコピーする。コピー後はintegrity_checkとforeign_key_checkで検証し、接続を閉じてからDBファイルのSHA-256を記録する。戻り値のforeign_keysにも検査値を返す。復元確認は別のDBへコピーし、整合性・外部キー・スキーマを検査する。元DBや旧経路は変更しない。

失敗した候補ファイルは成功したバックアップとして扱わず隔離・保持する。原因を解消し、新しい宛先で再実行する。元・宛先はエラー時にも閉じる。既存ファイルを削除して再試行しない。

DB・WAL・SHMはGit除外済み。data/backups/も全体を除外している。元ファイルの置換による実運用の切戻しはフェーズ2では行わない。専用サービスを止め、専用DBと付随ファイルを隔離し、既存運用を維持する。フェーズ1の復旧地点を保持する。

## 自動検証

```sh
python3 -m unittest discover -s scripts -p 'test_project_database*.py' -v
```

基本8テストと追加6テストを実行する。追加検証ではバックアップ呼出時の元・宛先PRAGMAを実測する。故障注入試験は意図的に外部キー有効化を妨げ、保存開始前に失敗することを確認する。

受入試験は別の一時プロジェクトとpipなしの新規venvへ基盤コードとスキーマだけをコピーし、CLI初期化・再実行・状態確認・空DBバックアップ復元とサービス起動停止を実行する。元DB・記事ファイル・実際の認証値は使わない。版更新は一時マイグレーション002を追加し、順次更新と新規環境での全版適用の結果を比較する。日時は試験内で固定して比較する。本番用の不要な002を追加するものではない。

新規venvは同じホスト上の独立したPython環境であり、別OS・別マシンの互換性試験を表すものではない。後続フェーズ開始時は最新のバックアップと入力基準値を改めて確認する。
