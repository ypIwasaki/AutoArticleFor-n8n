# 作業日・工程別のトークン記録

入口は `python3 scripts/autoarticle_ops.py tokens ...`。新しい外部AI呼び出しは行わず、
指定したCodexタスクのローカルログから使用量の数値だけを読み取る。
実際の運用工程を進める前に計測を開始する。過去の作業を推測して埋める機能ではない。

## 初回接続

現在のタスクID・Codexホームが環境に渡っている場合：

```bash
python3 scripts/autoarticle_ops.py tokens bind
```

現在の `CODEX_THREAD_ID` のログを探し、ログ先頭のタスクIDも照合する。
検索対象はファイル名であり、他の会話本文を検索しない。複数一致・不一致は停止する。
環境にIDが渡らないWindowsアプリ→WSL構成では、初回だけ現在のタスクのパスとIDを明示する。

```bash
python3 scripts/autoarticle_ops.py tokens bind --thread-id TASK_ID --session-log /mnt/c/Users/USER/.codex/sessions/YYYY/MM/DD/rollout-...-TASK_ID.jsonl
```

パスは実際に確認したものを使う。`--codex-home` で検索ルートを指定することもできる。
任意の環境変数は `AUTOARTICLE_CODEX_THREAD_ID`、`AUTOARTICLE_CODEX_SESSION_LOG`。
共通入口は既存の `.env` も読む（環境変数優先）。APIキーは不要。
現在の `CODEX_THREAD_ID` が存在する場合、他タスクへのbindや古いbindingの使用は拒否する。
IDが環境にない場合は明示選択したbindingに依存するため、**新しいタスクでは必ずbindし直す**。
同じプロジェクトを複数タスクで同時操作する場合は、呼び出し元のタスクIDも環境で渡す。
個人名入りパスをskillに埋め込まず、別PCでは接続だけ設定し直す。

bindは現在位置を基準にする。以前からのタスク累積値そのものを今回の使用量に加算しない。
再bindした同じタスクは既存の読取位置と履歴を再利用する。ログが取得できないbeginも工程を記録するが、値は未計測となる。
ログ未接続で記録した過去工程を、後から接続しただけで全量復元できるとは限らない。

## 通常フロー

記事対象日と作業日を分ける。以下の `--date` は記事対象日、ファイル名の日付は実際の作業日JST。

```bash
# 工程に必要なルールや本文を読む前に開始
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens begin summary

# 記事作業と検証後。既存checkpointが成功すると計測も閉じる
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint summary --evidence content/article-summaries/YYYY-MM-DD.md --note '実際に確認した内容と保留事項'

# 次の工程へ。別工程のbeginは直前の計測区間を閉じる
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens begin talent-review

# 成果物完成とは別に、計測だけを閉じる場合
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens end talent-review

# 依頼範囲を終了・中断するときに閉じる
python3 scripts/autoarticle_ops.py tokens finish

# 遅れて保存されたログを読み、既存レポートを再生成
python3 scripts/autoarticle_ops.py tokens report
python3 scripts/autoarticle_ops.py tokens report --work-date YYYY-MM-DD
```

工程名は `tokens begin --help` で確認できる。起動・収集・反映にも、それぞれbeginを付ける。
`apply` の一括反映を1回のコマンドで進める場合、Codex使用量はapply工程として記録する。
人材反映と分類反映を別々に計測したい場合は `apply-talent` / `apply-classification` と `apply --kind ...` を組み合わせる。
計測終了は運用の完了認定ではない。成果物・DB照合・表示確認は従来どおり必要。
checkpoint失敗時には計測を閉じない。計測側だけが失敗しても、成功済みの運用checkpointを取り消したり、反映を再実行したりしない。
`status` と `resume` は従来どおり読み取り専用で、計測ファイルも変更しない。

## 保存先

- `content/operation-usage/YYYY-MM-DD.md`：作業日別の確認表と計算根拠。
- `content/operation-usage/YYYY-MM-DD.json`：同じレポートの機械可読版。snapshotIdで内容を照合。
- `.operation-state/token-usage/`：ログ選択、読取位置、開始・終了、累積値と区間差分。Git対象外。

公開側のMD/JSONには会話本文・推論本文・ツール引数・認証情報・ログの絶対パスを入れない。
ローカル状態にも会話本文を複製しない。タスク識別には公開側でハッシュ化したsourceキーを使う。
MD/JSONはGit管理可能だが、コミット・プッシュは自動で行わない。別PCの生ログまでGitで共有する設計ではない。
既存の非生成Markdownを同名で見つけた場合、上書きせず停止する。生成レポートへの手書き追記は再生成で失われるため別ファイルに残す。

## 未割当の計算と割当条件

各観測区間について、次を計算する。

`使用量 = 後の累積トークン数 − 前の累積トークン数`

例：工程境界の前が合計1,000、後が1,300なら、区間使用量は300。
その区間が単一工程の開始・終了内に完全に収まれば、その工程へ割り当てる。
複数工程を跨ぐ場合、300という量は求められても「各工程が何トークンか」は差分だけでは分からないため、
前後の値・差分・候補工程・理由を残し、300を未割当に計上する。根拠のない按分は行わない。

途中の通知に欠落・不正形式があっても、前後の有効な累積値があれば差分を補完する。
これも単一工程に収まれば割当可能。ただし「欠落区間の差分補完」と明記し、
途中で未観測のカウンターリセットが起きていないことまでは保証しない。
前後の値がない場合や、確認できたカウンターが減少する場合は、推測値や負の使用量を記録しない。

開始前後を跨ぐ区間は、工程外の一部を含み得るため未割当とする。
終了後を跨ぐ区間は、後続の無関係な会話を合計へ含めないため**計算根拠だけを記録し、合計から除外**する。
finish後の最終返答を完全に捕捉する仕組みではない。生成したレポートは常に「観測できた範囲の暫定集計」とする。

## 数値の意味と制限

- 合計は入力＋出力。キャッシュ入力は入力、推論出力は出力の内訳として表示し、二重加算しない。
- `last_token_usage` を繰り返し合算しない。同じ累積値の通知・再読取は重複計上しない。
- 未通知・未接続・計測前の工程は未計測。観測区間のない工程を0と推定しない。
- Markdownの日付は使用量通知のJST日付。日跨ぎ差分は通知日の未割当に残し、時刻比率で分割しない。
- 入力には過去の会話・本文・ツール結果の再入力も含まれ得る。ファイルの文字数やコマンド出力サイズから推定した値ではない。
- ログ保存遅延があるため後日のreportで値が増える場合がある。サブエージェントや他タスクの使用量は勝手に合算しない。
- ログ形式が変わった場合、取得不可・警告として扱う。読取は初回末尾8MiB、以後は増分を1回約32MiBに制限し、残りはbacklogとして次回へ回す。
- ログの切詰め・置換・基準行の変更は停止する。履歴や状態を削除して無条件に計測をやり直さない。
- 課金額やアカウントの使用上限消費率を表すものではない。モデル・キャッシュ状況などが変わるため、トークン量だけで料金を判断しない。

公式にはタスク使用量の通知が定義されているが、この実装が読むローカルJSONLの形はバージョン依存のアダプター。
参考：[OpenAI Docs — App Serverの使用量通知](https://learn.chatgpt.com/docs/app-server#turn-events)。
