"""Calibrate the LLM judge against labelled cases (ADR-0026).

    make judge-calibrate        runs the judge (the real model) on evals/judge/calibration.json

For each split (tuning, held_out) and each criterion it reports:
    agreement       judge and label match
    unsafe passes   the label says FAIL, the judge says PASS: a bad explanation would go unnoticed
    false alarms    the label says PASS, the judge says FAIL: noisy, but safe
and lists every disagreement with the judge's reason. Results are saved to evals/judge/results/ and
shown on the demo UI's judge health panel. Re-run whenever the judge's prompt or model changes.
"""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from services.judge.judge import judge
from services.judge.rubric import PROMPT_VERSION, Criterion, JudgeCase

ROOT = Path(__file__).resolve().parent / "judge"
CONCURRENCY = 4


def score(cases: list[dict], outputs: dict[str, dict]) -> dict:
    """Agreement, unsafe passes, and false alarms per split and criterion, from judge outputs
    ({case id: {criterion: {"passed": bool, "reason": str}}})."""
    report = {}
    for split in sorted({c["split"] for c in cases}):
        in_split = [c for c in cases if c["split"] == split and c["id"] in outputs]
        per = {}
        for criterion in Criterion:
            pairs = [(c["labels"][criterion.value] == "PASS", outputs[c["id"]][criterion.value]["passed"]) for c in in_split]
            per[criterion.value] = {
                "cases": len(pairs),
                "agreement": round(sum(h == j for h, j in pairs) / len(pairs), 3) if pairs else None,
                "unsafe_passes": sum(1 for h, j in pairs if not h and j),
                "false_alarms": sum(1 for h, j in pairs if h and not j),
                "label_fails": sum(1 for h, _ in pairs if not h),
            }
        overall = [(all(c["labels"][k.value] == "PASS" for k in Criterion),
                    all(outputs[c["id"]][k.value]["passed"] for k in Criterion)) for c in in_split]
        report[split] = {
            "cases": len(in_split), "criteria": per,
            "overall_agreement": round(sum(h == j for h, j in overall) / len(overall), 3) if overall else None,
            "unsafe_passes": sum(per[k.value]["unsafe_passes"] for k in Criterion),
        }
    return report


def disagreements(cases: list[dict], outputs: dict[str, dict]) -> list[dict]:
    rows = []
    for c in cases:
        for criterion in Criterion:
            human = c["labels"][criterion.value]
            got = outputs.get(c["id"], {}).get(criterion.value)
            if got is not None and (human == "PASS") != got["passed"]:
                rows.append({"case": c["id"], "split": c["split"], "criterion": criterion.value, "label": human,
                             "judge": "PASS" if got["passed"] else "FAIL", "judge_reason": got["reason"],
                             "unsafe": human == "FAIL", "why_labelled": c["why"]})
    return rows


async def run(model, cases: list[dict]) -> dict[str, dict]:
    slots = asyncio.Semaphore(CONCURRENCY)

    async def one(c: dict) -> tuple[str, dict]:
        async with slots:
            out = await judge(model, JudgeCase(question=c["question"], answer=c["answer"], evidence=c["evidence"]))
            return c["id"], out.model_dump()

    return dict(await asyncio.gather(*(one(c) for c in cases)))


def main() -> int:
    from shared.llm import build_chat_model

    doc = json.loads((ROOT / "calibration.json").read_text())
    cases = doc["cases"]
    outputs = asyncio.run(run(build_chat_model(), cases))
    report = score(cases, outputs)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    now = datetime.now(UTC)
    result = {"meta": {"date": now.strftime("%Y-%m-%d %H:%M UTC"), "commit": commit, "labelled_by": doc["labelled_by"],
                       "judge_model": "gpt-4o 2024-11-20 (temperature 0)", "prompt_version": PROMPT_VERSION},
              "report": report, "disagreements": disagreements(cases, outputs), "outputs": outputs}
    out = ROOT / "results" / f"{now:%Y%m%d-%H%M}-{commit}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    for split, r in report.items():
        print(f"{split:9} {r['cases']:>2} cases | overall agreement {r['overall_agreement']:.0%} | "
              f"unsafe passes {r['unsafe_passes']}")
        for name, c in r["criteria"].items():
            print(f"   {name:13} agreement {c['agreement']:.0%}  unsafe passes {c['unsafe_passes']}/"
                  f"{c['label_fails']}  false alarms {c['false_alarms']}")
    for d in result["disagreements"]:
        print(f"  {'UNSAFE ' if d['unsafe'] else 'noisy  '} {d['split']:8} {d['case']:38} {d['criterion']:13} "
              f"label {d['label']} judge {d['judge']}: {d['judge_reason']}")
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
