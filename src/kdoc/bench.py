"""성능 측정: 내역서 추출 결과에 불일치를 '심어서' 검출률을 잰다 (정답 테스트셋, docs/PLAN_v1.md §3).

    python -m kdoc bench [--project P1] [--seed 42] [--per-type 3]

원본 파일은 건드리지 않는다. 시방서·내역서를 한 번 추출한 뒤 내역서 항목 목록의 사본을 바꿔 가며
공정 짝(사용자 CSV + 이름 규칙, AI 없음) → 비교를 다시 돌린다. 따라서 추출 이후 단계(짝·비교)의 성능이다.

심는 유형 (유형별로 후보를 모두 찾은 뒤 고정 시드로 뽑는다):
  자재 누락   짝 공종에서 시방서에도 있는 자재의 내역서 행을 지움      → '내역서에 없음' 이 새로 나와야 검출
  자재 추가   같은 공사의 다른 공종 행을, 그 자재가 시방서 짝 장에 없는 공종으로 옮겨 넣음 → '시방서에 없음'
  수치 변경   시방서와 같은 단위의 수치가 있는 내역서 행의 수치를 바꿈  → '수치 다름'
  공종 누락   짝이 있던 내역서 공종을 통째로 지움                    → 해당 시방서 장이 '짝' 이 아니게 됨

부작용: 심은 것과 관계없는 새 의심 항목이 생기면 따로 센다 (낮을수록 좋음).
"""

from __future__ import annotations

import copy
import json
import random
import time
from pathlib import Path

from . import DATA, RAW, compare, fetch, pairing, review
from .sections import PAIRS, group_chapters, group_sections, read_pairs

BENCH = DATA / "bench"
TYPES = ["자재 누락", "자재 추가", "수치 변경", "공종 누락"]


def _ck(clause: dict) -> tuple:
    return (clause["doc_id"], clause["chapter_no"], clause["chapter"])


def _sk(item: dict) -> tuple:
    return (item["doc_id"], item["section_code"])


def _keys(findings: list[dict]) -> set[tuple]:
    out = set()
    for f in findings:
        if f["kind"] == compare.KIND_NOT_IN_SPEC:
            out.add((f["kind"], f["material"], _sk(f["boq"]["item"])))
        else:
            out.add((f["kind"], f["material"], _ck(f["spec"]["clause"])))
    return out


def _change_value(item: dict) -> None:
    """비교 규칙이 읽는 첫 수치(숫자+단위)를 2배로 바꾼다. 예: '우레탄방수1mm' → '우레탄방수2mm'."""
    for field in ("spec", "name"):
        text = item.get(field) or ""
        for pat, _, _ in compare._UNIT_PATTERNS:
            m = pat.search(text)
            if m:
                a, b = m.span(1)
                item[field] = text[:a] + f"{float(m.group(1)) * 2:g}" + text[b:]
                return
    raise ValueError("수치가 없는 행")


class Project:
    """한 프로젝트의 추출 결과와 기준(심기 전) 결과."""

    def __init__(self, pid: str, materials: Path | None = None):
        self.pid = pid
        files, doc_ids = review.project_inputs(RAW, pid)
        t0 = time.perf_counter()
        ex = review.extract_inputs(files, doc_ids=doc_ids)
        self.extract_seconds = time.perf_counter() - t0
        self.clauses, self.items = ex["rows"]["clauses"], ex["rows"]["items"]
        self.chapters = group_chapters(self.clauses)
        pairs_file = PAIRS / f"{pid}.csv"
        self.manual = read_pairs(pairs_file) if pairs_file.exists() else []
        self.terms = compare.load_materials(materials)
        t0 = time.perf_counter()
        self.base = self.run(self.items)
        self.check_seconds = time.perf_counter() - t0

    def run(self, items: list[dict]) -> dict:
        pr = pairing.pair(self.chapters, group_sections(items), api_key=None, manual=self.manual)
        findings = compare.compare(pr["pairs"], self.clauses, items, self.terms)
        paired = {(c["doc_id"], c["chapter_no"], c["chapter"]) for c in self.chapters}
        paired &= {p["spec_key"] for p in pr["pairs"]}
        return {"pairs": pr["pairs"], "paired_chapters": paired, "keys": _keys(findings)}

    # ---------- 후보 ----------
    def _pair_map(self) -> dict[tuple, list[tuple]]:
        out: dict[tuple, list[tuple]] = {}
        for p in self.base["pairs"]:
            out.setdefault(p["spec_key"], []).append(p["boq_key"])
        return out

    def candidates(self) -> dict[str, list[dict]]:
        by_ch: dict[tuple, list[dict]] = {}
        for c in self.clauses:
            if c.get("chapter"):
                by_ch.setdefault(_ck(c), []).append(c)
        by_sec: dict[tuple, list[dict]] = {}
        for it in self.items:
            by_sec.setdefault(_sk(it), []).append(it)
        cands: dict[str, list[dict]] = {t: [] for t in TYPES}
        base = self.base["keys"]

        for ck, sks in self._pair_map().items():
            sf = compare.spec_facts(by_ch.get(ck, []), self.terms)
            items = [it for sk in sks for it in by_sec.get(sk, [])]
            bf = compare.boq_facts(items, self.terms)
            s_mats = {f["material"] for f in sf}
            b_mats = {f["material"] for f in bf}
            for m in sorted(s_mats & b_mats):
                if (compare.KIND_NOT_IN_BOQ, m, ck) not in base:
                    rows = [_sk(f["item"]) + (f["item"]["sheet"], f["item"]["row"]) for f in bf if f["material"] == m]
                    cands["자재 누락"].append({"chapter": ck, "material": m, "rows": sorted(set(rows))})
            # 수치 변경: 시방서 쪽에 같은 단위 수치가 있는 자재의 내역서 행
            for f in bf:
                m = f["material"]
                s_units = {u for x in sf if x["material"] == m for u, _ in x["values"]}
                units = [u for u, _ in f["values"] if u in s_units]
                if units and (compare.KIND_VALUE_DIFF, m, ck) not in base:
                    it = f["item"]
                    cands["수치 변경"].append({"chapter": ck, "material": m,
                                             "row": _sk(it) + (it["sheet"], it["row"])})
            # 공종 누락: 이 장의 유일한 짝 공종
            if len(sks) == 1:
                cands["공종 누락"].append({"chapter": ck, "section": sks[0]})

        # 자재 추가: 다른 공종의 실제 행을, 그 자재가 짝 장 시방서에 없는 공종으로 옮김
        sec_to_ch: dict[tuple, list[tuple]] = {}
        for ck, sks in self._pair_map().items():
            for sk in sks:
                sec_to_ch.setdefault(sk, []).append(ck)
        all_bf = compare.boq_facts(self.items, self.terms)
        for sk, cks in sec_to_ch.items():
            s_mats = {f["material"] for ck in cks for f in compare.spec_facts(by_ch.get(ck, []), self.terms)}
            here = {f["material"] for f in compare.boq_facts(by_sec.get(sk, []), self.terms)}
            seen = set()
            for f in all_bf:
                m, it = f["material"], f["item"]
                if m in s_mats or m in here or m in seen or _sk(it) == sk:
                    continue
                if len(compare.find_materials(f"{it.get('name') or ''} {it.get('spec') or ''}", self.terms)) != 1:
                    continue
                seen.add(m)
                cands["자재 추가"].append({"section": sk, "material": m,
                                         "row": _sk(it) + (it["sheet"], it["row"])})
        for t in cands:  # 재현 가능하도록 정렬
            cands[t].sort(key=lambda c: json.dumps(c, ensure_ascii=False, default=str))
        return cands

    # ---------- 심기 ----------
    def inject(self, kind: str, c: dict) -> tuple[list[dict], tuple]:
        items = copy.deepcopy(self.items)
        key = lambda it: _sk(it) + (it["sheet"], it["row"])  # noqa: E731
        if kind == "자재 누락":
            drop = set(map(tuple, c["rows"]))
            return [it for it in items if key(it) not in drop], (compare.KIND_NOT_IN_BOQ, c["material"], c["chapter"])
        if kind == "자재 추가":
            src = next(it for it in items if key(it) == tuple(c["row"]))
            target = next(it for it in items if _sk(it) == tuple(c["section"]))
            moved = {**copy.deepcopy(src), "doc_id": target["doc_id"], "section_code": target["section_code"],
                     "section": target["section"], "section_path": target["section_path"],
                     "sheet": target["sheet"], "row": 10**6}
            return items + [moved], (compare.KIND_NOT_IN_SPEC, c["material"], tuple(c["section"]))
        if kind == "수치 변경":
            it = next(it for it in items if key(it) == tuple(c["row"]))
            _change_value(it)
            return items, (compare.KIND_VALUE_DIFF, c["material"], c["chapter"])
        if kind == "공종 누락":
            return [it for it in items if _sk(it) != tuple(c["section"])], ("공정 짝 해제", c["chapter"])
        raise ValueError(kind)

    def evaluate(self, kind: str, c: dict) -> dict:
        items, expected = self.inject(kind, c)
        res = self.run(items)
        new = res["keys"] - self.base["keys"]
        chapter = expected[1] if kind == "공종 누락" else (c.get("chapter") or None)
        unpaired = bool(chapter) and chapter in self.base["paired_chapters"] and chapter not in res["paired_chapters"]
        if kind == "공종 누락":
            hit, how, side = unpaired, "공정 짝 해제" if unpaired else "", len(new)
        else:
            if expected in new:
                hit, how = True, expected[0]
            elif unpaired:  # 행을 지웠더니 공종이 통째로 비어 짝이 풀림 → 공정 짝 시트에서 드러남
                hit, how = True, "공정 짝 해제"
            else:
                hit, how = False, ""
            side = len(new - {expected})
        partial = (not hit and kind == "수치 변경"
                   and (compare.KIND_UNSURE, c["material"], c["chapter"]) in res["keys"])
        return {"type": kind, "target": c, "expected": list(map(str, expected)), "detected": hit,
                "detected_by": how, "flagged_unsure": partial, "side_effects": side}


def run(projects: list[str] | None = None, seed: int = 42, per_type: int = 3, out: Path | None = None) -> dict:
    ids = projects or [p["id"] for p in fetch.load_sources()]
    rng = random.Random(seed)
    cases, timing, baseline = [], {}, {}
    for pid in ids:
        proj = Project(pid)
        timing[pid] = {"추출(초)": round(proj.extract_seconds, 2), "짝+비교(초)": round(proj.check_seconds, 2)}
        baseline[pid] = len(proj.base["keys"])
        cands = proj.candidates()
        for kind in TYPES:
            pool = cands[kind]
            for c in rng.sample(pool, min(per_type, len(pool))):
                r = proj.evaluate(kind, c)
                r["project"], r["candidates"] = pid, len(pool)
                cases.append(r)
    table = {}
    for kind in TYPES:
        rs = [c for c in cases if c["type"] == kind]
        table[kind] = {"심음": len(rs), "검출": sum(c["detected"] for c in rs),
                       "검출률": round(sum(c["detected"] for c in rs) / len(rs), 3) if rs else None,
                       "판단 불가로만 표시": sum(c["flagged_unsure"] for c in rs),
                       "부작용(건)": sum(c["side_effects"] for c in rs)}
    total = len(cases)
    hits = sum(c["detected"] for c in cases)
    result = {"seed": seed, "per_type": per_type, "projects": ids,
              "summary": {"심음": total, "검출": hits, "검출률": round(hits / total, 3) if total else None},
              "by_type": table, "timing": timing, "baseline_findings": baseline, "cases": cases}
    out = out or BENCH
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "results.md").write_text(to_markdown(result), encoding="utf-8")
    return result


def to_markdown(r: dict) -> str:
    lines = [f"# kdoc 검출률 측정 (seed {r['seed']}, 유형별 프로젝트당 최대 {r['per_type']}건)", "",
             "| 유형 | 심음 | 검출 | 검출률 | 판단 불가로만 표시 | 부작용(관계없는 새 의심) |",
             "|---|---|---|---|---|---|"]
    for kind, t in r["by_type"].items():
        rate = "후보 없음" if t["검출률"] is None else f"{t['검출률']:.0%}"
        lines.append(f"| {kind} | {t['심음']} | {t['검출']} | {rate} | {t['판단 불가로만 표시']} | {t['부작용(건)']} |")
    s = r["summary"]
    lines += [f"| **전체** | {s['심음']} | {s['검출']} | {(s['검출률'] or 0):.0%} | | |", "",
              "| 프로젝트 | 추출(초) | 짝+비교(초) | 기준 의심 건수 |", "|---|---|---|---|"]
    for pid, t in r["timing"].items():
        lines.append(f"| {pid} | {t['추출(초)']} | {t['짝+비교(초)']} | {r['baseline_findings'][pid]} |")
    lines += ["", "## 놓친 사례", ""]
    for c in r["cases"]:
        if not c["detected"]:
            note = " (판단 불가로는 표시)" if c["flagged_unsure"] else ""
            lines.append(f"- {c['project']} {c['type']}: {' / '.join(c['expected'])}{note}")
    return "\n".join(lines) + "\n"
