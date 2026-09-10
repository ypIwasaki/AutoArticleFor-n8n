"""Recomputable daily reports. Attribute only intervals contained in one step."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid

from autoarticle_progress import Blocked, JST, read_json, timestamp, write_json
from codex_usage_log import FIELDS

MARKER = "<!-- autoarticle-token-usage v1 -->"
NAMES = {"preflight": "実行前チェック", "investigation": "障害調査", "n8n": "n8n起動", "collect": "収集", "summary": "記事要約", "talent-review": "人材索引",
         "classification-review": "記事分類", "keywords": "キーワード", "weekly": "週次レポート",
         "apply": "DB反映", "apply-talent": "人材DB反映", "apply-classification": "分類DB反映",
         "dashboard": "確認アプリ起動", "page": "画面確認", "common": "共通作業", "prepare": "事前確認",
         "capture": "本文取得", "shared-review": "共通本文確認", "report": "報告", "unassigned": "未割当"}


def day(value):
    return timestamp(value).astimezone(JST).date().isoformat()


def overlaps(start, end, left, right):
    if start == end:
        return left <= start and (right is None or start < right)
    return end > left and (right is None or start < right)


def allocation(source, sample):
    start, end = timestamp(sample["from"]), timestamp(sample["at"])
    windows = [w for w in source["windows"] if overlaps(start, end, timestamp(w["start"]), timestamp(w["end"]) if w["end"] else None)]
    if not windows:
        return None  # The rest of this conversation is not this operation's usage.
    run_dates = sorted({w["runDate"] for w in windows})
    run_date = run_dates[0] if len(run_dates) == 1 else "multiple"
    contained = [w for w in windows if timestamp(w["start"]) <= start and (w["end"] is None or end <= timestamp(w["end"]))]
    spans = [s for s in source["spans"] if s["windowId"] in {w["id"] for w in windows} and overlaps(start, end, timestamp(s["start"]), timestamp(s["end"]) if s["end"] else None)]
    exact = [s for s in spans if timestamp(s["start"]) <= start and (s["end"] is None or end <= timestamp(s["end"]))]
    result = {"step": "unassigned", "runDate": run_date, "reason": "step_boundary",
              "candidates": sorted({s["step"] for s in spans}), "spanId": None,
              "includedInTotals": True, "workDate": day(sample["at"])}
    closed_ends = [timestamp(w["end"]) for w in windows if w["end"]]
    if len(closed_ends) == len(windows) and end > max(closed_ends):
        # Do not inflate this operation with subsequent conversation usage. Keep
        # the computable boundary difference as evidence, outside its totals.
        result.update(includedInTotals=False, reason="after_finish_boundary_excluded", workDate=day(max(w["end"] for w in windows)))
    elif day(sample["from"]) != day(sample["at"]):
        result["reason"] = "cross_day_notification_date"
    elif len(contained) != 1:
        result["reason"] = "measurement_window_boundary"
    elif len(exact) == 1 and len(spans) == 1:
        result.update(step=exact[0]["step"], spanId=exact[0]["id"], reason="contained_in_step")
    elif not spans:
        result.update(step="common", reason="between_steps")
    # Numeric recovery is independent of stage attribution: gap intervals retain
    # their cumulative difference, but are not presented as directly metered calls.
    result["measurement"] = "derived_across_gap" if sample.get("gap") else "observed_counter_difference"
    return result


def report_dates(sources, at):
    dates = set()
    for source in sources:
        for window in source["windows"]:
            dates.add(day(window["start"]))
            dates.add(day(window["end"] or at))
        for sample in source["samples"]:
            assigned = allocation(source, sample)
            if assigned:
                dates.add(assigned["workDate"])
    return sorted(dates)


def summed(samples):
    # No samples means unknown, not a fabricated zero. Missing detail remains null.
    return {field: sum(s["tokens"][field] for s in samples) if samples and all(s["tokens"].get(field) is not None for s in samples) else None for field in FIELDS}


def build_report(sources, work_date, at):
    if dt.date.fromisoformat(work_date).isoformat() != work_date:
        raise Blocked("invalid_work_date")
    rows, intervals, used_sources = {}, [], []
    def row_for(run_date, step):
        key = (run_date, step)
        if key not in rows:
            rows[key] = {"runDate": run_date, "step": step, "attempts": 0, "open": False, "samples": []}
        return rows[key]
    for source in sources:
        relevant = False
        for span in source["spans"]:
            if day(span["start"]) <= work_date <= day(span["end"] or at):
                row = row_for(span["runDate"], span["step"])
                row["attempts"] += 1
                row["open"] = row["open"] or span["end"] is None
                relevant = True
        for sample in source["samples"]:
            assigned = allocation(source, sample)
            if assigned is None or assigned["workDate"] != work_date:
                continue
            interval = dict(assigned, source=source["key"], offset=sample["offset"], start=sample["from"], end=sample["at"],
                            tokens=sample["tokens"], before=sample["before"], after=sample["after"], calculation="after_minus_before")
            intervals.append(interval)
            if assigned["includedInTotals"]:
                row_for(assigned["runDate"], assigned["step"])["samples"].append(interval)
            relevant = True
        if relevant:
            used_sources.append({"source": source["key"], "status": source["sourceStatus"], "lastReadAt": source["lastReadAt"],
                                 "issues": sorted({i["reason"] for i in source["issues"]})})
    result_rows = []
    for key in sorted(rows):
        row = rows[key]
        samples = row.pop("samples")
        row["tokens"] = summed(samples)
        row["sampleCount"] = len(samples)
        row["derivedIntervals"] = sum(s["measurement"] == "derived_across_gap" for s in samples)
        row["status"] = "observed_partial" if samples else "unmeasured"
        result_rows.append(row)
    result = {"reportKind": "autoarticle-token-usage", "schemaVersion": 1, "workDate": work_date, "timezone": "Asia/Tokyo",
              "status": "provisional_observations", "rows": result_rows, "knownTotals": summed([i for i in intervals if i["includedInTotals"]]), "intervals": intervals,
              "sources": used_sources, "scope": "registered_windows_with_boundary_intervals; final_reply_after_finish_excluded",
              "dayPolicy": "usage_notification_day; cross-day intervals remain unassigned",
              "detailPolicy": "cached input and reasoning output are breakdowns, not additional totals"}
    result["snapshotId"] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result["generatedAt"] = at
    return result


def number(value):
    return "未計測" if value is None else format(value, ",")


def render(report):
    lines = [MARKER, "# 工程別トークン使用量 — " + report["workDate"], "",
             "- 作業日：%s（JST）。記事の対象日は各行に別記。" % report["workDate"],
             "- 計測状態：観測値の暫定集計。課金明細や全工程の厳密な消費量ではありません。",
             "- 集計日時：" + report["generatedAt"], "- スナップショット：`" + report["snapshotId"] + "`", "",
             "| 記事対象日 | 工程 | 試行 | 入力 | キャッシュ入力（内数） | 出力 | 推論出力（内数） | 合計 | 計測状態 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in report["rows"]:
        tokens = row["tokens"]
        state = "観測差分" if row["sampleCount"] else "未計測"
        if row["derivedIntervals"]:
            state += "・欠落区間の差分補完あり"
        if row["open"]:
            state += "・計測中"
        lines.append("| %s | %s | %d | %s | %s | %s | %s | %s | %s |" % (row["runDate"], NAMES[row["step"]], row["attempts"],
                     number(tokens["input_tokens"]), number(tokens["cached_input_tokens"]), number(tokens["output_tokens"]),
                     number(tokens["reasoning_output_tokens"]), number(tokens["total_tokens"]), state))
    if not report["rows"]:
        lines.append("| — | 工程記録なし | — | 未計測 | 未計測 | 未計測 | 未計測 | 未計測 | begin未実施 |")
    totals = report["knownTotals"]
    lines += ["", "## 観測できた分の合計", "",
              "入力 %s ／出力 %s ／合計 %s トークン。未計測分は含みません。" % (number(totals["input_tokens"]), number(totals["output_tokens"]), number(totals["total_tokens"])),
              "キャッシュ書込入力（取得できた場合の内数）：%s。" % number(totals["cache_write_input_tokens"]), "",
              "## 未割当・差分補完の根拠", "",
              "使用量 = 区間後の累積値 − 区間前の累積値。入力・出力も同じ方法で計算し、キャッシュ・推論を重複加算しません。",
              "一つの工程に完全に含まれる区間はその工程へ割当。境界をまたぎ配分を確定できない区間は、数量を計算したうえで未割当として保持します。", "",
              "| 区間（UTC等、ログの時刻） | 振分先 | 前の累積合計 | 後の累積合計 | 差分 | 理由 |",
              "| --- | --- | ---: | ---: | ---: | --- |"]
    evidence = [i for i in report["intervals"] if i["step"] == "unassigned" or i["measurement"] == "derived_across_gap"]
    for interval in evidence:
        lines.append("| %s → %s | %s | %s | %s | %s | %s%s |" % (interval["start"], interval["end"], NAMES[interval["step"]],
                     number(interval["before"]["total_tokens"]), number(interval["after"]["total_tokens"]), number(interval["tokens"]["total_tokens"]),
                     interval["reason"], (" / 集計対象外" if not interval["includedInTotals"] else "") + (" / 欠落通知を跨ぐ差分" if interval["measurement"] == "derived_across_gap" else "")))
    if not evidence:
        lines.append("| — | — | — | — | — | 該当する観測区間なし |")
    lines += ["", "## 計測範囲と注意", "",
              "- 入力には過去の会話・ツール結果等の再入力も含まれ得ます。Python/n8nの実行時間やアカウント全体の使用率ではありません。",
              "- beginより前、finishより後の作業は対象外。開始境界を跨ぐ差分は範囲外の一部を含む未割当、終了境界を跨ぐ差分は計算根拠だけを示して合計から除外します。",
              "- finish後の最終返答は含みません。遅延通知は後日のtokens reportで再集計できますが、通知不足を0にしません。",
              "- 日跨ぎ区間は通知日の未割当に計上します。時間比率による按分は行いません。",
              "- カウンターリセット・ログ置換・形式不明は警告。欠落区間を差分補完できても、途中の未観測リセットまでは保証できません。",
              "- このファイルは自動生成。会話本文・認証情報・ローカルログの絶対パスは含めません。", ""]
    for source in report["sources"]:
        lines.append("- %s：%s（最終読取 %s）、警告：%s" % (source["source"], source["status"], source["lastReadAt"] or "未読取", ", ".join(source["issues"]) or "なし"))
    return "\n".join(lines) + "\n"


def save_report(root, report):
    folder = root / "content/operation-usage"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (report["workDate"] + ".md")
    data_path = path.with_suffix(".json")
    if path.exists() and not path.read_text(encoding="utf-8").startswith(MARKER):
        raise Blocked("refusing_to_overwrite_non_generated_usage_markdown")
    if data_path.exists() and read_json(data_path).get("reportKind") != "autoarticle-token-usage":
        raise Blocked("refusing_to_overwrite_non_generated_usage_json")
    write_json(data_path, report)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            stream.write(render(report))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()
    return path
