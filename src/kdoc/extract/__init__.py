"""문서 유형별 추출기: pdf(공통 페이지), spec(시방서 조항), drawing(도면 문자), boq(내역서 항목)."""

import re


def compact(s: str) -> str:
    """'방 수 공 사' → '방수공사' 처럼 공백 제거 (제목 매칭용)."""
    return re.sub(r"\s+", "", s or "")
