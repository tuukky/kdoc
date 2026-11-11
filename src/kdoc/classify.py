"""첨부 파일명 → 문서 종류 (PLAN_v2 §4.2, 사전 등록 규칙 — 결과를 본 뒤 고치지 않는다).

순서대로 처음 맞는 규칙을 쓴다.
  1 .hwp .hwpx                               → unsupported (HWP)
  2 '시방' + .pdf                            → spec
  3 ('내역' | '설계서' | '산출') + .xls .xlsx → boq
  4 '도면' + .pdf                            → drawing
  5 '공고'                                   → notice
  6 .zip                                     → zip (압축을 풀어 안의 파일에 1~5 적용, 한 단계만)
  7 그 밖                                    → other
"""

from __future__ import annotations

import re
from pathlib import PurePath

SPEC, BOQ, DRAWING, NOTICE, ZIP, OTHER, UNSUPPORTED = "spec", "boq", "drawing", "notice", "zip", "other", "unsupported"
REVIEWABLE = (SPEC, BOQ, DRAWING)


def classify(name: str) -> str:
    suffix = PurePath(name).suffix.lower()
    if suffix in (".hwp", ".hwpx"):
        return UNSUPPORTED
    if "시방" in name and suffix == ".pdf":
        return SPEC
    if any(k in name for k in ("내역", "설계서", "산출")) and suffix in (".xls", ".xlsx"):
        return BOQ
    if "도면" in name and suffix == ".pdf":
        return DRAWING
    if "공고" in name:
        return NOTICE
    if suffix == ".zip":
        return ZIP
    return OTHER


def original_name(stored: str) -> str:
    """fetch 가 붙인 '02_spec_' 접두어를 떼어 원래 파일명으로."""
    return re.sub(r"^\d{2}_[a-z]+_", "", stored)


def readiness(files: list[dict]) -> tuple[bool, str]:
    """files: [{'doc_type': ...}] → (검토 가능?, 사유). 시방서·내역서가 1개 이상씩 있어야 한다."""
    types = [f["doc_type"] for f in files]
    has_spec, has_boq = SPEC in types, BOQ in types
    if has_spec and has_boq:
        return True, ""
    if not files:
        return False, "첨부 없음"
    if types and all(t == UNSUPPORTED for t in types):
        return False, "HWP만 있음"
    if not has_spec and not has_boq:
        return False, "시방서·내역서 없음"
    return False, "시방서 없음" if not has_spec else "내역서 없음"
