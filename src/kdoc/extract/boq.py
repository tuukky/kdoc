"""내역서(공내역서) 엑셀 → 항목 목록.

조달 공내역서는 내역 프로그램에서 내보낸 형식이라 시트·열 위치가 파일마다 조금씩 다르다.
그래서 열 번호를 고정하지 않고, 상단 10행 안에서 '품명·규격·단위·수량' 머리글을 찾아 열을 정한다.

- 공종 행: 품명 칸이 '010101  가 설 공 사' 처럼 [숫자코드 + 공종명] 이고 단위·수량이 비어 있음
- 항목 행: 단위 또는 수량이 있음
- 합계 행('[ 합 계 ]', '[ 소 계 ]')은 건너뜀
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from . import compact

HEADER_KEYS = {"name": "품명", "spec": "규격", "unit": "단위", "qty": "수량", "note": "비고"}
RE_SECTION = re.compile(r"^\s*(\d{2,12})\s+(\S.*)$")
RE_TOTAL = re.compile(r"^\[\s*[합소총]\s*계\s*\]$")


def detail_sheets(sheet_names: list[str]) -> list[str]:
    """'공종별내역서', '내역서' 같은 세부 내역 시트만 (집계표·일위대가·단가 제외)."""
    out = []
    for n in sheet_names:
        c = compact(n)
        if "내역" in c and not any(k in c for k in ("집계", "일위", "단가", "목록")):
            out.append(n)
    return out


def find_header(df: pd.DataFrame, max_rows: int = 10) -> tuple[int, dict] | None:
    for r in range(min(max_rows, len(df))):
        cols = {}
        for c, v in enumerate(df.iloc[r].tolist()):
            if pd.isna(v):
                continue
            cv = compact(str(v))
            for key, label in HEADER_KEYS.items():
                if key not in cols and cv == label:
                    cols[key] = c
            if "section_code" not in cols and cv in ("공종코드", "공종"):
                cols["section_code"] = c
        if {"name", "unit", "qty"} <= cols.keys():
            return r, cols
    return None


def _cell(row, col):
    if col is None:
        return None
    v = row[col] if col < len(row) else None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def parse_sheet(df: pd.DataFrame) -> list[dict]:
    found = find_header(df)
    if not found:
        return []
    hr, cols = found
    items = []
    path: list[tuple[str, str]] = []  # (code, name) 공종 계층

    for r in range(hr + 1, len(df)):
        row = df.iloc[r].tolist()
        name = _cell(row, cols["name"])
        unit = _cell(row, cols["unit"])
        qty = _num(_cell(row, cols["qty"]))
        if not name or RE_TOTAL.match(compact(name)) or compact(name) in ("단가", "금액"):
            continue
        m = RE_SECTION.match(name)
        if m and unit is None and qty is None:
            code, title = m.group(1), compact(m.group(2))
            # 코드가 더 짧거나 같은 상위 공종은 잘라낸다 (코드 접두어 기반 계층)
            path = [p for p in path if code.startswith(p[0]) and len(p[0]) < len(code)]
            path.append((code, title))
            continue
        if unit is None and qty is None:
            continue
        items.append({
            "row": r + 1,  # 엑셀 행 번호 (1부터)
            "section_code": path[-1][0] if path else _cell(row, cols.get("section_code")),
            "section": path[-1][1] if path else None,
            "section_path": [p[1] for p in path],
            "name": name,
            "spec": _cell(row, cols.get("spec")),
            "unit": unit,
            "qty": qty,
            "note": _cell(row, cols.get("note")),
        })
    return items


def read_boq(path: Path) -> list[dict]:
    xs = pd.read_excel(path, sheet_name=None, header=None)
    out = []
    for sheet in detail_sheets(list(xs)):
        for it in parse_sheet(xs[sheet]):
            out.append({"sheet": sheet, **it})
    return out
