# Windows＋WSLへのPC移行

対象は現在の **WSL/Ubuntu＋npm＋専用SQLite DB** の運用です。
目標は「クローン → 実行環境セットアップ → 初回だけ移行データを復元 → 通常運用」。
空のDBから新しい業務環境を作る手順ではありません。現在のDB・n8n状態を引き継ぎます。

## Gitと初回移送の分担

| Gitで共有 | Gitには入れず初回に移送 |
| --- | --- |
| Python、画面、ワークフロー定義、運用スキル、ルール | `data/autoarticle.sqlite`：業務データ・本文・判断履歴・読書き設定 |
| `config/`、追跡済みの`content/` | `.env`：APIキー、DBサービストークン等 |
| `runtime/package.json`と`package-lock.json`、`.nvmrc` | `~/.n8n/`：内部DB、認証の暗号鍵、ワークフローID・公開状態等 |
| セットアップ・移行コマンドと本書 | `.operation-state/`、Git対象外を含む`content/`、`config/` |

移行ツールは`content/`と`config/`を丸ごと含めます。復元先の同内容ファイルはそのまま使います。
プロセスIDファイルとトークン計測の現在のbindingだけは移送から除外し、履歴や
`submission_unknown`などの未確定状態は保持します。`.operation-logs/`、n8nイベントログ、
実行環境本体、Git履歴は含みません。独自に置いた上記以外の業務ファイルは別途確認してください。

移行データには秘密情報と記事本文が入ります。`.migration/`はGit対象外です。
移送には暗号化された媒体・転送経路を使い、公開リポジトリへ追加しないでください。
ツール自身は暗号化しません。ハッシュは破損検出用であり、送信者の認証ではありません。

## 1. この整備をGitHubへ反映する

新PCで取得できるよう、今回追加したコード・ロックファイル・手順書をコミットしてpushします。
既存の設定や記事の変更も整理してください。exportは未コミット変更・未追跡ファイルがある場合に停止します
（Git除外ファイルはこの制限の対象外）。自動commit・pushは行いません。

移行データにはその時点のコミットIDが記録されます。新PCは同じコミットで復元します。
移行中に別コミットへ進んだ場合は、まず記録されたコミットへcheckoutしてください。

## 2. 旧PCで書込みを止める

収集・レビュー保存・提案反映・画面からの評価を完了または中断状態として確認し、
n8n、DBサービス、ダッシュボード、レビューアプリ、本文取得などのプロセスを終了します。
Windows側のアプリや自動実行も確認します。実行中の業務を強制終了するコマンドは本書では用意しません。

ツールはWSLの関連プロセスとローカルポートを確認しますが、Windows側や外部からの書込みまで
完全には検出できません。`--writers-stopped`はそれらを含めて停止確認済みであることの指定です。
n8nに`new`/`running`/`waiting`の実行が残っている場合もexportを拒否します。
該当実行は内容を確認してから解決してください。

**データの書き出し後、旧PCで業務を再開しないでください。** 再開した場合は、再停止して新しい移行データを作ります。
旧PCと新PCの両方で同じ定期収集を動かさないようにします。

## 3. 旧PCで移行データを作る

WSLのプロジェクトルートで実行します。出力先は毎回新しい名前にします。

```bash
python3 scripts/pc_migration.py export \
  --output .migration/pc-transfer-20260918 \
  --writers-stopped
python3 scripts/pc_migration.py verify --bundle .migration/pc-transfer-20260918
```

日付部分は実際の移行日に置き換えてください。
専用DBとn8n内部DBはSQLite backup APIでWALを含む独立したスナップショットを作り、
`integrity_check`を実施します。保存前後の元ファイルのSHA-256・ファイル集合を比較し、
途中で変化した場合は不完全な出力として残し、成功扱いにしません。
ツールはワークフローの公開状態や暗号鍵を変更しません。

表示された`manifestSha256`を移行データとは別に控えてください。
`pc-transfer-20260918`ディレクトリ全体を新PCへ転送し、verifyで同じ値になることを確認します。
秘密値は標準出力へ表示しません。作成・検証にはDB・本文全体の読み取りが必要なので時間がかかります。
移行元には移行対象サイズ以上、新PCにはそのコピーと復元先・n8n依存関係の空き容量が必要です。

`INCOMPLETE`が残る出力は使用できません。原因を解消して別名へexportしてください。
既存出力を上書き・自動削除しません。

## 4. 新PCの前提環境

- Windows上のWSL/Ubuntu。プロジェクトは`~/N8N/AutoArticleFor-n8n`等、WSLのLinuxファイルシステムに置きます。
- Git、Python 3.8以上、Bash、curl、Node.js **22.12.0**、npm **11.16.0**。
- 移行先ユーザーの`~/.n8n`は未作成であること。既存n8nがある場合は別のWSLユーザー・ディストリビューションを使います。
- 現行のn8nは **2.8.4**。移行とバージョン更新を同時に行いません。

現在の稼働環境はUbuntu 20.04/Python 3.8です。新しいUbuntu/Pythonでの本番移行は
最終確認が必要です。`fcntl`を使用するため、PythonコマンドはWindows側ではなくWSLで実行します。
CPUアーキテクチャが変わる場合やコミュニティノードにネイティブ依存がある場合は、その再構築も必要です。

GitHubのリポジトリURLを使ってクローンします。

```bash
git clone YOUR_REPOSITORY_URL
cd AutoArticleFor-n8n
```

Nodeのバージョン管理にnvmを導入済みなら、`.nvmrc`を利用できます。

```bash
nvm install
nvm use
npm install -g npm@11.16.0
bash scripts/setup_environment.sh
```

nvmを使わない場合も、指定バージョンのNode/npmをPATHで選択してからセットアップします。
セットアップは`npm ci`で`runtime/node_modules/`へ導入します。インターネット接続が必要です。
個人のグローバルn8n、`.env`、DB、ワークフローには変更を加えず、サービスも起動しません。
インストール時のpeer dependency/deprecation警告は上流依存由来です。終了コードと後続のdoctorで確認します。

固定値は`config/runtime-versions.json`、`.nvmrc`、`runtime/package.json`/`package-lock.json`にあります。
ロックファイルは今回解決した依存集合です。旧PCのグローバルインストールに含まれる全依存の完全な複製ではありません。

## 5. 新PCで検証・復元する

`.env.example`を`.env`へコピーする必要はありません。旧PCの`.env`を復元します。
以下の`/path/to/pc-transfer-20260918`は実際に転送したディレクトリに置き換えます。

```bash
python3 scripts/pc_migration.py verify --bundle /path/to/pc-transfer-20260918
# 表示されるmanifestSha256を旧PCで控えた値と照合
# 現在のコミットが違う場合だけ、verifyが表示するコミットへcheckout
# git checkout COMMIT_FROM_VERIFY
python3 scripts/pc_migration.py restore \
  --bundle /path/to/pc-transfer-20260918 \
  --writers-stopped
python3 scripts/pc_migration.py doctor
```

復元は同じコミットのクリーンなクローンに限定します。`.env`、専用DB、進捗フォルダー、
`~/.n8n`が存在する場合は上書きしません。既存の異なる内容のファイルやシンボリックリンクも拒否します。
全ファイルの検証と競合確認が終わってから書込みを開始します。ディスク不足などで書込み途中に
失敗した場合、`.migration/RESTORE_INCOMPLETE`を残し、通常の起動コマンドも起動を拒否します。既存ファイルを削除せず、
新しいクローン・未使用のWSLユーザー環境へ復元し直してください。

doctorはオフラインで実行環境・設定項目・専用DBの読み取り経路を確認します。
全項目成功は起動前の設定確認であり、APIキーの有効期限や画面表示を保証するものではありません。
本移行ツールは標準配置専用です。カスタムDBパス・n8nユーザーフォルダー・プロセス環境で指定した
暗号鍵を検出した場合は停止し、別途その環境に合わせた移行が必要になります。

## 6. 新PCで再開する

**旧PCのn8nが停止したままであることを確認してから起動します。**
復元されたワークフローの有効状態も保持されるので、起動後は元の定期実行が再開し得ます。

```bash
python3 scripts/autoarticle_ops.py start n8n
python3 scripts/autoarticle_ops.py status
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD resume
python3 scripts/autoarticle_ops.py start dashboard
```

`YYYY-MM-DD`は引き継ぐ作業日へ置き換えます。通常の入口は`autoarticle_ops.py`に統一します。
`start n8n`は`.env`のDBサービス設定を読み込み、DBサービスを用意してn8nを起動します。
起動スクリプト単体を呼ぶだけではDBサービス準備が揃いません。
起動時にはプロジェクト内の固定版n8nを優先し、従来環境にはグローバル版のフォールバックを残しています。

確認する内容：

- `status`のAPI接続、ワークフロー定義一致、有効状態、スケジュール、タイムゾーン。
- `resume`の未完了・結果不明の処理。移行を理由に収集や提案を再送しません。
- [確認アプリ](http://127.0.0.1:8765/)で対象日、記事、評価、レポートが参照できること。
- Codexのトークン計測を使う場合、新PCの今回のタスクへ`tokens bind`で接続し直すこと。

API認証が失敗する場合はn8nでAPIキーの有効性を確認して`.env`を更新し、再照合します。
復元直後のワークフローを再インポートして別IDを作る必要はありません。
移行先で業務データを更新した後は、旧PCへ単純に戻すと新しい更新が失われます。
切戻す場合も最新データを停止状態で移送してください。

## Docker・過去のセットアップ資料について

現在の`docker-compose.yml`はn8n単体の旧構成です。専用DBサービスはWSLの
`127.0.0.1`に限定しているため、Composeを起動するだけでは現在の業務構成になりません。
今回の移行ではDockerを使用しません。コンテナ対応はDBサービスの接続・起動構成を含む別作業です。
READMEや`docs/n8n-setup.md`の旧Data Tables関連説明は互換経路の参考情報です。
Dドライブにあった過去のバックアップを再作成する必要はありません。
今回の`.migration/`は明示的なPC移行用の一時出力であり、常設バックアップを復活させるものではありません。

## 開発時の検証

```bash
python3 -m unittest discover -s tests -p test_pc_migration.py -v
bash -n scripts/setup_environment.sh scripts/start_n8n_with_file_access.sh
```

テストは一時ディレクトリの小規模DB・n8n状態で往復復元、WAL、入力変化、破損、
既存データ・リンク・パス逸脱の拒否を検証します。本番の移送・別PCでの運用再開は別途実施します。
