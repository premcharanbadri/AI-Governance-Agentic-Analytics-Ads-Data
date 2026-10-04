"""Compare two question runs side by side (before and after a change).

  python governance/compare_runs.py 20261004-0214-b9cd 20261004-0930-1a2b
  python governance/compare_runs.py --latest            # the two most recent runs

Questions are matched by their text, so editing or reordering questions.txt between runs is fine.
Writes governance/reports/compare_<A>_vs_<B>.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "governance" / "logs" / "question_runs"


def load(run: str) -> dict[str, dict]:
    path = Path(run) if run.endswith(".jsonl") else RUNS / f"{run}.jsonl"
    if not path.exists():
        sys.exit(f"Run not found: {path}. Available: {sorted(p.stem for p in RUNS.glob('*.jsonl'))}")
    return {r["question"]: r for r in map(json.loads, path.read_text().splitlines())}


def summary(rs: list[dict]) -> dict:
    flag = lambda pred: sum(any(pred(f) for f in r["flags"]) for r in rs)
    return {
        "questions": len(rs),
        "with no flags": sum(not r["flags"] for r in rs),
        "empty answers": flag(lambda f: f == "EMPTY ANSWER"),
        "never got data": flag(lambda f: f == "resolved a metric but never retrieved data"
                               or f.endswith("query matched no data")),
        "rejected / invalid tool calls": sum(sum(x["decision"] in ("rejected_arguments", "invalid_input")
                                                 for x in r["audit"]) for r in rs),
        "policy denials": sum(sum(x["decision"] == "denied" for x in r["audit"]) for r in rs),
        "PII in answers": flag(lambda f: f.startswith("PII IN ANSWER")),
        "total time (s)": round(sum(r["seconds"] for r in rs)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--latest", action="store_true")
    a = ap.parse_args()
    if a.latest:
        runs = sorted(RUNS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)[-2:]
        if len(runs) < 2:
            sys.exit("Need at least two runs in governance/logs/question_runs/")
        a.runs = [p.stem for p in runs]
    if len(a.runs) != 2:
        sys.exit("Give two run IDs, or use --latest")
    A, B = (load(r) for r in a.runs)
    both = [q for q in A if q in B]
    sa, sb = summary([A[q] for q in both]), summary([B[q] for q in both])

    out = [f"# Run comparison: `{a.runs[0]}` (A) vs `{a.runs[1]}` (B)", "",
           f"{len(both)} questions in both runs. Flags are automatic signals; the review columns are yours.", "",
           "| Measure | A | B |", "|---|---|---|"]
    out += [f"| {k} | {sa[k]} | {sb[k]} |" for k in sa]
    out += ["", "| ID | Question | A flags | B flags | Review A | Review B |", "|---|---|---|---|---|---|"]
    for q in both:
        fa, fb = "; ".join(A[q]["flags"]) or "-", "; ".join(B[q]["flags"]) or "-"
        out.append(f"| {B[q]['id']} | {q[:60]} | {fa} | {fb} | | |")
    out += ["", "## Answers", ""]
    for q in both:
        calls = lambda r: ", ".join(f"{x['tool']} ({x['decision']})" for x in r["audit"]) or "no tools"
        out += [f"### {B[q]['id']}: {q}", "", f"**Good answer:** {B[q].get('expected', '')}", "",
                f"**A** ({calls(A[q])}):", "", "> " + A[q]["answer"].replace("\n", "\n> "), "",
                f"**B** ({calls(B[q])}):", "", "> " + B[q]["answer"].replace("\n", "\n> "), ""]
    path = ROOT / "governance" / "reports" / f"compare_{a.runs[0]}_vs_{a.runs[1]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))
    print("\n".join(f"{k:32} {sa[k]:>6} -> {sb[k]}" for k in sa))
    print(f"\nReport: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
