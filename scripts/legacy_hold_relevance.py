"""Conservative, source-grounded adapters for held reviews predating missingTopics.

This is a bounded parser, not semantic AI classification. Unsupported or ambiguous
reasons are explicitly undecidable. Its immutable assessment is separate from reviews.
"""
import json
import re
import unicodedata
import article_review_facts as shared

VERSION = "legacy-hold-relevance-v1"
ALIASES = {
    "article-summary": ("article-summary", "要約"),
    "talent-index": ("talent-index", "人材", "人材確認", "人物確認"),
    "article-classification": ("article-classification", "分類"),
}
# Specific attributes prevent a generic name/topic match from reopening a hold.
ATTRIBUTES = {
    "affiliation": ("所属", "事務所", "勤務先"),
    "contract-duration": ("契約期間", "契約の期間"),
    "contract-start": ("契約開始日", "契約の開始日"),
    "event-date": ("開催日", "開催日時", "出演日", "出演日時"),
    "amount": ("参加費", "料金", "金額"),
}
UNCERTAIN = re.compile(r"不明|未確認|未定|不確|不足|できない|推測|見込み|予定|可能性|かもしれ|ではない|未所属|所属なし|未契約|退所|脱退|退職|元所属|かつて")
DATE = r"(?:[0-9]{4}年)?[0-9]{1,2}月[0-9]{1,2}日"

def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))

def reasons_for(record, task):
    dedicated = record.get("holds", {}).get(task, {}).get("reason")
    if dedicated:
        return [dedicated]
    specific, unscoped = [], []
    for reason in record.get("unresolved", []):
        matched = None
        for stage, aliases in ALIASES.items():
            if any(re.match(r"^\s*" + re.escape(alias) + r"\s*[:：]", reason) for alias in aliases):
                matched = stage
                break
        if matched == task:
            specific.append(reason.split(":", 1)[-1] if ":" in reason else reason.split("：", 1)[-1])
        elif matched is None:
            unscoped.append(reason)
    if specific:
        return specific
    held = [stage for stage, status in record["taskStatus"].items() if status == "held"]
    return unscoped if held == [task] else []

def criteria(record, task):
    reasons = reasons_for(record, task)
    if not reasons:
        return None, "保留理由を対象工程に対応付けられない"
    text = normalized("；".join(reasons))
    attrs = [key for key, words in ATTRIBUTES.items() if any(normalized(word) in text for word in words)]
    if len(attrs) != 1:
        return None, "不足項目が未対応または複数あり、関連性を一意に判定できない"
    # Use the actual historical grounded entities; do not invent an entity from the new evidence.
    names = sorted({e["name"] for e in record.get("entities", [])
                    if e["kind"] == "person" and e.get("factIds")})
    named = [name for name in names if normalized(name) in text]
    if len(named) == 1:
        subject = named[0]
    elif not named and len(names) == 1:
        subject = names[0]
    else:
        return None, "過去の人物根拠から保留対象を一意に特定できない"
    return dict(subject=subject, attribute=attrs[0], reasons=reasons), None

def values(quote, criterion, organizations):
    text = normalized(quote)
    if UNCERTAIN.search(text):
        return set()
    attribute = criterion["attribute"]
    if attribute == "affiliation":
        # Both the organization and an affirmative relationship must be quoted.
        if not re.search(r"所属|在籍|勤務", text):
            return set()
        return {normalized(name) for name in organizations if re.search(
            re.escape(normalized(name)) + r"(?:に|の)?(?:所属|在籍|勤務)|(?:所属(?:先)?|勤務先)(?:は|:|：)?" + re.escape(normalized(name)), text)}
    if attribute == "contract-duration":
        return {n + unit.replace("年間", "年").replace("か月", "月").replace("ヶ月", "月").replace("月間", "月").replace("日間", "日")
                for n, unit in re.findall(r"契約(?:の)?(?:期間)?(?:は|:|：)(?:約|最長|最低|最大)?([0-9]+)(年間?|か月|ヶ月|月間?|日間?)", text)}
    if attribute == "contract-start":
        return set(re.findall(r"契約(?:の)?開始日(?:は|:|：)(" + DATE + ")", text))
    if attribute == "event-date":
        return set(re.findall(r"(?:開催日(?:時)?|出演日(?:時)?)(?:は|:|：)(" + DATE + ")", text))
    if attribute == "amount":
        return {number.replace(",", "") + "円" for number in re.findall(r"(?:参加費|料金|金額)(?:は|:|：)([0-9][0-9,]*)円", text)}
    return set()

def claims(record, criterion, task=None):
    # task is used only for new input. Baseline considers all already known facts,
    # so moving facts between taskFacts or changing IDs cannot manufacture novelty.
    facts = record.get("facts", [])
    if task and task in record.get("taskFacts", {}):
        selected = set(record["taskFacts"][task])
        facts = [fact for fact in facts if fact["id"] in selected]
    evidence = {e["id"]: e for e in record.get("evidence", [])}
    subject = criterion["subject"]
    signatures = {}
    for fact in facts:
        entities = [e for e in record.get("entities", []) if fact["id"] in e.get("factIds", [])]
        bound = any(e["name"] == subject for e in entities)
        organizations = [e["name"] for e in entities if e["kind"] == "organization"]
        for eid in fact["evidenceIds"]:
            e = evidence[eid]
            if any(other["kind"]=="person" and other["name"]!=subject and normalized(other["name"]) in normalized(e["quote"]) for other in record.get("entities", [])):
                continue
            if not bound and normalized(subject) not in normalized(e["quote"]):
                continue
            for value in values(e["quote"], criterion, organizations):
                signature = shared.digest([normalized(subject), criterion["attribute"], value])
                signatures.setdefault(signature, []).append(dict(factId=fact["id"], evidenceId=eid))
    return signatures

def unparsed_candidates(record, criterion):
    evidence = {e["id"]: e for e in record.get("evidence", [])}
    unresolved = []
    for fact in record.get("facts", []):
        entities = [e for e in record.get("entities", []) if fact["id"] in e.get("factIds", [])]
        if not any(e["name"] == criterion["subject"] for e in entities):
            continue
        organizations = [e["name"] for e in entities if e["kind"] == "organization"]
        for eid in fact["evidenceIds"]:
            quote = evidence[eid]["quote"]
            if any(normalized(word) in normalized(quote) for word in ATTRIBUTES[criterion["attribute"]]) and not values(quote, criterion, organizations):
                unresolved.append(dict(factId=fact["id"], evidenceId=eid))
    return unresolved

def validate_snapshot(c, row):
    snap = c.execute("SELECT * FROM review_input_snapshots WHERE review_id=?", (row["id"],)).fetchone()
    if not snap:
        return False
    try:
        shared.validate_record(json.loads(row["raw_json"]), json.loads(snap["article_json"]),
                               json.loads(snap["capture_json"]), row["rule_hash"])
    except (KeyError, TypeError, ValueError):
        return False
    return True

def assess(c, article_id, task, held_row, current_row):
    previous, current = json.loads(held_row["raw_json"]), json.loads(current_row["raw_json"])
    reason = "; ".join(previous.get("unresolved", []))
    criterion, failure = criteria(previous, task)
    assessment = dict(version=VERSION, articleId=article_id, task=task,
                      heldReviewId=held_row["id"], currentReviewId=current_row["id"],
                      heldContentVersionId=held_row["content_version_id"],
                      currentContentVersionId=current_row["content_version_id"],
                      criterion=criterion, addedEvidence=[], status="held", explanation="")
    if not validate_snapshot(c, held_row) or not validate_snapshot(c, current_row):
        failure = "当時または追加材料の本文版・引用根拠を検証できない"
    if failure:
        assessment.update(status="undetermined", explanation=failure)
    else:
        before, after = claims(previous, criterion), claims(current, criterion, task)
        added = sorted(set(after) - set(before))
        assessment["addedEvidence"] = [ref for signature in added for ref in after[signature]]
        assessment["materialSignatures"] = dict(previous=sorted(before), current=sorted(after))
        if added:
            assessment.update(status="resumed", explanation="過去の不足項目と対象人物に対応する新しい引用根拠が追加された")
        else:
            unresolved = unparsed_candidates(current, criterion)
            if unresolved:
                assessment.update(status="undetermined",
                                  explanation="不足項目に触れる引用はあるが、未確定表現または未対応の表記で値を断定できない",
                                  unparsedEvidence=unresolved)
            else:
                assessment["explanation"] = "不足項目に対応する新しい確認可能な材料なし"
    public = dict(state="resumed" if assessment["status"] == "resumed" else "held", reason=reason)
    if assessment["status"] == "resumed":
        public["newMaterialCount"] = len(set(after) - set(before))
    # The normal packet schema remains unchanged; detailed uncertainty is in DB audit.
    return public, assessment

def held_rows(c, article_id, task):
    rows = c.execute("SELECT * FROM review_records WHERE article_id=? ORDER BY rowid", (article_id,)).fetchall()
    held = [row for row in rows if json.loads(row["raw_json"])["taskStatus"].get(task) == "held"]
    return (held[-1], rows[-1]) if held else (None, None)
