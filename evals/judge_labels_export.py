"""Export people's re-assessments of real disputes as calibration cases for the judge (ADR-0026).

    make judge-labels-export      (reads the deployed intake API through a port-forward)

Each label becomes a case in the calibration format: the exact question, answer and evidence the
judge saw (kept with its verdict), and the PERSON's pass/fail per criterion as the label. Review
them, then move the useful ones into evals/judge/calibration.json, as tuning or held-out cases.
"""

import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services.demo_ui.logic import LoginSettings, login_reviewer  # noqa: E402

OUT = Path(__file__).resolve().parent / "judge" / "production_labels.json"


def to_cases(labels: list[dict]) -> list[dict]:
    cases = []
    for label in labels:
        case = ((label.get("judgement") or {}).get("result") or {}).get("case")
        if not case:
            continue  # judged before the case was kept: nothing to replay
        cases.append({
            "id": f"prod-{str(label['dispute_id'])[:8]}", "split": "production", "source": label.get("kind"),
            "question": case["question"], "answer": case["answer"], "evidence": case["evidence"],
            "labels": {k: "PASS" if v else "FAIL" for k, v in label["human_verdict"].items()},
            "why": label.get("note") or "", "labelled_by": label.get("resolved_by"),
        })
    return cases


def main() -> int:
    url = os.environ.get("SUPERVISOR_URL", "http://127.0.0.1:18004")
    token = login_reviewer("ops-ana", LoginSettings.from_env())
    response = httpx.get(f"{url}/v1/reviews/human-labels", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    response.raise_for_status()
    cases = to_cases(response.json())
    OUT.write_text(json.dumps({"about": "People's re-assessments of real disputes, exported for review.",
                               "cases": cases}, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(cases)} labelled case(s) written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
