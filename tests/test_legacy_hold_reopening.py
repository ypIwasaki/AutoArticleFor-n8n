"""One legacy-held article, four agreed transitions; no full regression run."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import ai_minimization_fixture as fx
import ai_input_minimization as mini
import article_review_facts as shared
import project_business_writes as business
import project_database as db
import project_readers as project
import read_ai_inputs as inputs

OUT=db.ROOT/".operation-state/ai-input-minimization/followup"

class LegacyHoldReopening(unittest.TestCase):
    def test_one_article_four_transitions(self):
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="legacy-hold-",dir=OUT) as temporary:
            root=fx.setup(Path(temporary));path=project.path_for(root)
            def submit(operation,kind,payload):
                return business.submit(operation,kind,payload,path,root)
            def payload(task="talent-index"):
                return inputs.build_payload(root,fx.DAY,task,limit=1)
            def audit():
                with project.reader(root) as reader:
                    return [json.loads(x[0]) for x in reader.c.execute(
                        "SELECT raw_json FROM legacy_history_records WHERE kind='ai-legacy-hold-assessment' ORDER BY rowid")]
            fx.save_review(root)
            # The existing normal ready input and additional read stay byte-for-byte unchanged.
            compared=False
            baseline=OUT/"before-ai_input_minimization.py"
            if baseline.exists():
                spec=importlib.util.spec_from_file_location("before_minimization",baseline)
                before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
                normal_before=before.build_payload(root,fx.DAY,"talent-index",limit=1)
                normal_after=mini.build_payload(root,fx.DAY,"talent-index",limit=1)
                self.assertEqual(normal_before,normal_after)
                ref=normal_after["articles"][0]["ref"]
                self.assertEqual(before.additional(root,fx.DAY,"talent-index",ref,"body",6000,1000),
                                 mini.additional(root,fx.DAY,"talent-index",ref,"body",6000,1000))
                compared=True
            # Preserve an existing saved stage while the legacy talent stage stays unfinished.
            summary_ref=payload("article-summary")["articles"][0]["ref"]
            submit("legacy-fixture-summary","summary",dict(day=fx.DAY,summaries=[dict(ref=summary_ref,text="星野アキが9月20日に出演。参加費は3000円。")]))
            legacy=fx.review(root)
            legacy["taskStatus"].update({"talent-index":"held","article-classification":"held"})
            legacy["unresolved"]=["talent-index: 星野アキの契約期間が不明",
                                  "article-classification: 主題を判断する情報が不足"]
            self.assertNotIn("holds",legacy)
            fx.save_review(root,legacy,"legacy-original-hold")
            with project.reader(root) as reader:
                aid=reader.c.execute("SELECT article_id FROM review_records ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                original=[tuple(x) for x in reader.c.execute("SELECT id,raw_json FROM review_records ORDER BY rowid")]
                original_snapshots=[tuple(x) for x in reader.c.execute("SELECT * FROM review_input_snapshots ORDER BY rowid")]
            states=[]
            def check_held(number,label):
                p=payload()
                self.assertEqual(p["excluded"],{"held":1})
                self.assertEqual(p["articles"],[])
                states.append(dict(number=number,label=label,status="passed",result="held"))
                return p
            # 1. No new decision material, including date/rule/hash/wording changes.
            check_held(1,"材料追加なし")
            changed=copy.deepcopy(legacy)
            changed["taskStatus"]["talent-index"]="ready"
            changed["reviewedAt"]="2026-09-18T01:05:00Z"
            changed["facts"][0]["text"]="星野アキの出演と参加費を確認した。"
            rule=root/shared.RULES;rule.write_text(rule.read_text()+"\n検証用のルール更新\n")
            changed["policyHash"]=shared.policy_hash(root)
            fx.save_review(root,changed,"legacy-metadata-only")
            self.assertEqual(payload()["excluded"],{"held":1})
            # 2. A genuinely new quote about the venue, unrelated to the held contract.
            unrelated=copy.deepcopy(changed)
            quote="背景資料として会場の設備を説明する。";start=fx.BODY.index(quote)
            unrelated["facts"].append(dict(id="venue",text=quote,evidenceIds=["venue-e"]))
            unrelated["evidence"].append(dict(id="venue-e",field="contentText",start=start,end=start+len(quote),quote=quote))
            unrelated["entities"][0]["factIds"].append("venue")
            fx.save_review(root,unrelated,"legacy-unrelated-material")
            check_held(2,"無関係な材料追加")
            # 3. New grounded contract period, without any topics/missingTopics.
            related=copy.deepcopy(unrelated)
            quote="追加根拠：出演契約の期間は1年間である。";start=fx.BODY.index(quote)
            related["facts"].append(dict(id="contract",text="星野アキの出演契約期間は1年間。",evidenceIds=["contract-e"]))
            related["evidence"].append(dict(id="contract-e",field="contentText",start=start,end=start+len(quote),quote=quote))
            related["entities"][0]["factIds"].append("contract")
            fx.save_review(root,related,"legacy-related-material")
            reopened=payload()
            self.assertEqual(reopened["returnedArticles"],1)
            self.assertEqual(reopened["articles"][0]["resume"]["state"],"resumed")
            self.assertNotIn("content",reopened["articles"][0])
            self.assertEqual(payload("article-summary")["excluded"],{"saved":1})
            self.assertEqual(payload("article-classification")["excluded"],{"held":1})
            states.append(dict(number=3,label="関連材料追加",status="passed",result="talent-index only resumed"))
            # 4. The operator still lacks sufficient evidence; preserve this as a new hold.
            reheld=copy.deepcopy(related)
            reheld["taskStatus"]["talent-index"]="held"
            reheld["unresolved"][0]="talent-index: 星野アキの契約期間が不明（追加根拠だけでは不十分）"
            fx.save_review(root,reheld,"legacy-reheld")
            check_held(4,"再開後に再保留")
            replay=copy.deepcopy(related);replay["reviewedAt"]="2026-09-19T01:05:00Z"
            replay["facts"][-1]["text"]="出演契約の期間は1年とされる。"
            fx.save_review(root,replay,"legacy-same-material-again")
            self.assertEqual(payload()["excluded"],{"held":1})
            self.assertEqual(payload()["excluded"],{"held":1})
            evaluations=audit()
            self.assertTrue(any(x["status"]=="resumed" and x["addedEvidence"]==[dict(factId="contract",evidenceId="contract-e")] for x in evaluations))
            unknown=[x for x in evaluations if x["task"]=="article-classification"]
            self.assertTrue(unknown and all(x["status"]=="undetermined" and x["explanation"] for x in unknown))
            count=len(evaluations);payload();self.assertEqual(len(audit()),count)
            with project.reader(root) as reader:
                for ident,raw in original:
                    self.assertEqual(reader.c.execute("SELECT raw_json FROM review_records WHERE id=?",(ident,)).fetchone()[0],raw)
                for snap in original_snapshots:
                    self.assertEqual(tuple(reader.c.execute("SELECT * FROM review_input_snapshots WHERE review_id=?",(snap[0],)).fetchone()),snap)
                self.assertEqual(reader.c.execute("SELECT count(*) FROM content_fetch_attempts").fetchone()[0],1)
                self.assertEqual(reader.c.execute("SELECT count(*) FROM article_summaries").fetchone()[0],1)
            result=dict(status="passed",representative="synthetic-event-1",states=states,
                assertions=dict(oldReviewsAndSnapshotsUnchanged=True,savedStageExcluded=True,onlyTalentStageResumed=True,
                    sameMaterialAfterReholdStaysHeld=True,undeterminedReasonPersisted=True,noRefetch=True,
                    noArtifactRegeneration=True,normalInputAndAdditionalReferenceUnchanged=True if compared else None,
                    repeatedReadAddsNoDuplicateAssessment=True),
                assessmentCount=count,method="Existing SQLite business/read adapters in a temporary isolated root",
                normalInputComparison="byte-equivalent JSON objects before/after; prior token measurements retained" if compared else "not_run: saved baseline unavailable",
                reusedPreviousSixPatterns=True,fullSixPatternRerun=False)
            (OUT/"legacy-hold-verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False))
if __name__=="__main__":unittest.main()
