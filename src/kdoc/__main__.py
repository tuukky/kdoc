"""CLI: python -m kdoc {fetch|extract|stats|sections} [--project P1]
     python -m kdoc review --spec a.pdf --boq b.xlsx [--drawing c.pdf] [--out 폴더]   (화면 프로그램과 같은 검토)
     python -m kdoc review --project P1                                              (받아둔 공고 파일로 검토)"""

from __future__ import annotations

import argparse
import json
import sys

from . import fetch


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kdoc")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "extract", "stats", "sections"):
        sp = sub.add_parser(name)
        sp.add_argument("--project", help="P1 같은 프로젝트 ID (생략 시 전체)")
    rv = sub.add_parser("review", help="시방서↔내역서 공정 짝 + 양방향 자재·수치 비교 → 결과 엑셀")
    rv.add_argument("--project", help="data/raw/{P} 의 파일로 검토")
    rv.add_argument("--spec", nargs="*", default=[])
    rv.add_argument("--boq", nargs="*", default=[])
    rv.add_argument("--drawing", nargs="*", default=[])
    rv.add_argument("--out", help="결과 폴더 (기본 data/review/{P 또는 local})")
    rv.add_argument("--no-ai", action="store_true", help="AI 없이 사용자 지정 및 이름 일치로 공정 짝")
    args = p.parse_args(argv)

    if args.cmd == "review":
        return _review(args)

    projects = fetch.load_sources()
    if args.project:
        projects = [x for x in projects if x["id"] == args.project]

    if args.cmd == "fetch":
        ok = True
        for proj in projects:
            recs = fetch.fetch_project(proj)
            for r in recs:
                status = "ERROR " + r["error"] if "error" in r else f"{r['size']:>10,}  {r['file']}"
                ok &= "error" not in r
                print(f"{proj['id']} seq{r['seq']:<2} {status}")
        return 0 if ok else 1

    from . import pipeline  # 추출 의존성은 필요할 때만 로드

    if args.cmd == "extract":
        for proj in projects:
            summary = pipeline.extract_project(proj["id"])
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "stats":
        for proj in projects:
            print(json.dumps(pipeline.stats(proj["id"]), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "sections":
        from . import sections

        for proj in projects:
            print(json.dumps(sections.write_listing(proj["id"]), ensure_ascii=False, indent=2))
        return 0
    return 1


def _review(args) -> int:
    from pathlib import Path

    from . import RAW, pairing, review
    from .sections import PAIRS, REVIEW

    files = {"spec": [Path(x) for x in args.spec], "boq": [Path(x) for x in args.boq],
             "drawing": [Path(x) for x in args.drawing]}
    doc_ids, pairs_file = {}, None
    if args.project:
        if args.project not in {p["id"] for p in fetch.load_sources()}:
            print(f"알 수 없는 프로젝트: {args.project}", file=sys.stderr)
            return 1
        try:
            registered, doc_ids = review.project_inputs(RAW, args.project)
        except review.ProjectNotReady as exc:
            print(exc, file=sys.stderr)
            return 1
        for doc_type, paths in registered.items():
            files[doc_type] = list(dict.fromkeys([*(p.resolve() for p in files[doc_type]), *paths]))
        pairs_file = PAIRS / f"{args.project}.csv"
    out = Path(args.out) if args.out else REVIEW / (args.project or "local")
    key = None if args.no_ai else pairing.load_key()
    res = review.run_review(files, out, api_key=key, doc_ids=doc_ids, pairs_file=pairs_file,
                            progress=lambda m, f: print(f"[{f:4.0%}] {m}", file=sys.stderr))
    print(json.dumps({"excel": res["excel"], **{k: v for k, v in res["summary"].items() if k != "문서"}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
