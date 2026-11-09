"""공정 짝 안에서 시방서 ↔ 내역서 자재·수치를 양방향으로 비교한다 (규칙 기반, AI 없음).

- 자재 이름: 자재 사전(config/materials.txt)에 있는 것만 알아본다.
- 수치: 숫자 + 단위(mm·회·겹·MPa)가 함께 있을 때만. THK3MM, 9.5T 는 mm 로 본다.
  시방서는 같은 줄의 자재에, 자재 없는 줄의 수치는 바로 위 2줄 안의 자재에 붙인다.
  내역서는 한 행(품명+규격) 안의 자재에 붙인다.
- 결과 종류: 내역서에 없음(시방서 기준) · 시방서에 없음(내역서 기준) · 수치 다름.
  판정이 아니라 '의심 위치'다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import CONFIG
from .extract import compact

KIND_NOT_IN_BOQ = "내역서에 없음"
KIND_NOT_IN_SPEC = "시방서에 없음"
KIND_VALUE_DIFF = "수치 다름"
KIND_UNSURE = "판단 불가"

_PLACES = re.compile(r"외벽|내벽|벽체|바닥|천장|지붕|옥상|기둥|수직|수평")
_CONDITIONS = re.compile(r"비노출|노출|실내|실외|습식|건식")
_LIMIT = re.compile(r"이상|이하|초과|미만|\d\s*(?:mm|cm|㎜|T)?\s*[~∼～–×x*+-]\s*\d|±", re.I)


def _context(text: str) -> tuple:
    return (tuple(sorted(set(_PLACES.findall(text)))), tuple(sorted(set(_CONDITIONS.findall(text)))))

_NUM = r"(\d+(?:\.\d+)?)"
# 순서 중요: THK 를 먼저 잡아 지운 뒤 일반 mm 를 잡는다 (같은 숫자 두 번 세지 않게)
_UNIT_PATTERNS = [
    (re.compile(rf"THK\s*[:=]?\s*{_NUM}\s*(?:mm|㎜)?", re.I), "mm", 1.0),
    (re.compile(rf"{_NUM}\s*(?:mm|㎜)", re.I), "mm", 1.0),
    (re.compile(rf"{_NUM}\s*cm(?![²2])", re.I), "mm", 10.0),
    (re.compile(rf"{_NUM}\s*T(?![a-zA-Z])"), "mm", 1.0),
    (re.compile(rf"{_NUM}\s*MPa", re.I), "MPa", 1.0),
    (re.compile(rf"{_NUM}\s*회"), "회", 1.0),
    (re.compile(rf"{_NUM}\s*겹"), "겹", 1.0),
]


def materials_path() -> Path:
    """EXE로 실행하면 EXE 옆의 materials.txt (사용자가 고칠 수 있게), 아니면 config/materials.txt."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "materials.txt"
    return CONFIG / "materials.txt"


def load_materials(path: Path | None = None) -> list[tuple[str, str]]:
    """[(찾을 표현(공백 없음·소문자), 대표 이름)] — 긴 표현부터."""
    path = path or materials_path()
    terms: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, aliases = line.partition(":")
        name = name.strip()
        for t in [name, *aliases.split(",")]:
            key = compact(t).lower()
            if key:
                terms.setdefault(key, name)
    return sorted(terms.items(), key=lambda kv: -len(kv[0]))


def find_materials(text: str, terms: list[tuple[str, str]]) -> list[str]:
    """문장 안 자재(대표 이름). 긴 표현이 먼저 잡히고, 잡힌 자리는 다시 쓰지 않는다."""
    s = compact(text).lower()
    found: list[str] = []
    for key, name in terms:
        if key in s:
            s = s.replace(key, "\0")
            if name not in found:
                found.append(name)
    return found


def find_values(text: str) -> list[tuple[str, float]]:
    """[(단위, 값)] — 예: 'THK3MM, 2회' → [('mm', 3.0), ('회', 2.0)]."""
    s = text or ""
    out: list[tuple[str, float]] = []
    for pat, unit, scale in _UNIT_PATTERNS:
        for m in pat.finditer(s):
            out.append((unit, round(float(m.group(1)) * scale, 2)))
        s = pat.sub(" ", s)
    return out


def fmt_value(unit: str, v: float) -> str:
    return f"{v:g}{unit}"


def spec_facts(clauses: list[dict], terms) -> list[dict]:
    """시방서 조항 → 줄 단위 사실 [{material, values, line, clause}]."""
    facts = []
    for c in clauses:
        lines = [l.strip() for l in f"{c.get('heading') or ''}\n{c.get('text') or ''}".splitlines() if l.strip()]
        recent = None
        for i, line in enumerate(lines):
            mats, vals = find_materials(line, terms), find_values(line)
            if mats:
                recent = (i, mats, line)
                for m in mats:
                    facts.append({"material": m, "values": vals, "line": line, "clause": c,
                                  "context": _context(line), "ambiguous": len(mats) > 1 or bool(_LIMIT.search(line))})
            elif vals and recent and i - recent[0] <= 2:
                _, materials, previous = recent
                inherited = _context(previous)
                own = _context(line)
                for m in materials:
                    facts.append({"material": m, "values": vals, "line": line, "clause": c,
                                  "context": tuple(a or b for a, b in zip(own, inherited)),
                                  "ambiguous": len(materials) > 1 or bool(_LIMIT.search(line))})
    return facts


def boq_facts(items: list[dict], terms) -> list[dict]:
    facts = []
    for it in items:
        text = f"{it.get('name') or ''} {it.get('spec') or ''}"
        materials = find_materials(text, terms)
        for m in materials:
            facts.append({"material": m, "values": find_values(text), "line": text.strip(), "item": it,
                          "context": _context(text), "ambiguous": len(materials) > 1 or bool(_LIMIT.search(text))})
    return facts


def _by_unit(facts: list[dict]) -> dict[str, set[float]]:
    out: dict[str, set[float]] = {}
    for f in facts:
        for unit, v in f["values"]:
            out.setdefault(unit, set()).add(v)
    return out


def _buckets(facts, unit):
    out = {}
    for f in facts:
        vals = {v for u, v in f["values"] if u == unit}
        if not vals:
            continue
        context = f["context"]
        # 여러 부위 또는 조건이 한 줄에 섞이면 개별 수치의 귀속을 알 수 없다.
        if f["ambiguous"] or any(len(part) > 1 for part in context):
            context = None
        values, evidence = out.setdefault(context, (set(), []))
        values.update(vals)
        evidence.append(f)
    return out


def _compare_values(material, spec, boq, findings, coverage):
    for unit in sorted(_by_unit(spec).keys() | _by_unit(boq).keys()):
        left, right = _buckets(spec, unit), _buckets(boq, unit)
        unmatched = ([], [])
        for context in dict.fromkeys((*left, *right)):
            sv, sf = left.get(context, (set(), []))
            bv, bf = right.get(context, (set(), []))
            if context is None or not sf or not bf:
                unmatched[0].extend(sf)
                unmatched[1].extend(bf)
                continue
            # 한쪽에만 나타난 부위는 다른 부위의 수치와 비교하지 않는다.
            # 양쪽에 부위가 없으면 단일 수치끼리만 비교할 수 있다.
            label = " / ".join(part[0] for part in context if part) or "부위·조건 미지정"
            if len(sv) == len(bv) == 1:
                coverage["수치 비교"] += 1
                if sv == bv:
                    coverage["수치 일치"] += 1
                    continue
                kind, reason = KIND_VALUE_DIFF, f"{label}: 같은 조건에서 단일 수치가 다름"
            else:
                kind, reason = KIND_UNSURE, f"{label}: 같은 부위·조건의 단일 수치 쌍을 특정할 수 없음"
            findings.append({
                "kind": kind, "material": material, "spec": sf[0], "boq": bf[0],
                "spec_value": ", ".join(fmt_value(unit, v) for v in sorted(sv)),
                "boq_value": ", ".join(fmt_value(unit, v) for v in sorted(bv)),
                "reason": reason,
            })
        if any(unmatched):
            sf, bf = unmatched
            findings.append({
                "kind": KIND_UNSURE, "material": material, "spec": (sf or spec)[0], "boq": (bf or boq)[0],
                "spec_value": ", ".join(fmt_value(unit, v) for v in sorted(_by_unit(sf).get(unit, ()))),
                "boq_value": ", ".join(fmt_value(unit, v) for v in sorted(_by_unit(bf).get(unit, ()))),
                "reason": "부위·조건 또는 단위가 대응하지 않거나 범위·한계치가 포함되어 확인 필요",
            })


def compare(pairs: list[dict], clauses: list[dict], items: list[dict], terms, *, coverage=None) -> list[dict]:
    """pairs: 짝 목록 [{"spec_key": (doc_id, chapter_no, chapter), "boq_key": (doc_id, section_code)}].
    한 장이 여러 공종과, 한 공종이 여러 장과 짝일 수 있다 (2:1, 1:2)."""
    ch_clauses: dict[tuple, list[dict]] = {}
    for c in clauses:
        if c.get("chapter"):
            ch_clauses.setdefault((c["doc_id"], c["chapter_no"], c["chapter"]), []).append(c)
    sec_items: dict[tuple, list[dict]] = {}
    for it in items:
        sec_items.setdefault((it["doc_id"], it["section_code"]), []).append(it)

    ch_to_sec: dict[tuple, list[tuple]] = {}
    sec_to_ch: dict[tuple, list[tuple]] = {}
    for p in pairs:
        ch_to_sec.setdefault(p["spec_key"], []).append(p["boq_key"])
        sec_to_ch.setdefault(p["boq_key"], []).append(p["spec_key"])

    findings: list[dict] = []
    if coverage is None:
        coverage = {}
    coverage.update({"시방서 자재 비교": 0, "내역서 자재 비교": 0, "수치 비교": 0, "수치 일치": 0})

    # 시방서 기준: 장의 자재가 짝 공종(들)에 있는가, 수치가 맞는가
    for ck, sks in ch_to_sec.items():
        sf = spec_facts(ch_clauses.get(ck, []), terms)
        b_items = [it for sk in sks for it in sec_items.get(sk, [])]
        bf = boq_facts(b_items, terms)
        b_mats = {f["material"] for f in bf}
        seen: set[str] = set()
        for f in sf:
            m = f["material"]
            if m in seen:
                continue
            seen.add(m)
            coverage["시방서 자재 비교"] += 1
            if m not in b_mats:
                findings.append({"kind": KIND_NOT_IN_BOQ, "material": m, "spec": f,
                                 "boq_first": min(b_items, key=lambda x: (x["sheet"], x["row"])) if b_items else None})
                continue
            _compare_values(m, [x for x in sf if x["material"] == m], [x for x in bf if x["material"] == m],
                            findings, coverage)

    # 내역서 기준: 공종의 자재가 짝 장(들)에 있는가
    for sk, cks in sec_to_ch.items():
        s_clauses = [c for ck in cks for c in ch_clauses.get(ck, [])]
        s_mats = {f["material"] for f in spec_facts(s_clauses, terms)}
        seen = set()
        for f in boq_facts(sec_items.get(sk, []), terms):
            m = f["material"]
            if m in seen:
                continue
            seen.add(m)
            coverage["내역서 자재 비교"] += 1
            if m in s_mats:
                continue
            findings.append({"kind": KIND_NOT_IN_SPEC, "material": m, "boq": f,
                             "spec_first": min(s_clauses, key=lambda c: c["page_start"]) if s_clauses else None})
    return findings
