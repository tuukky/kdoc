"""시방서: 페이지 텍스트를 장(章)·조항 단위로 나눈다.

결과 조항마다 편·장 제목·조항 번호·본문·시작/끝 페이지를 남겨서,
나중에 비교 결과에 "시방서 방수공사 3.2 (p.18)" 같은 근거를 붙일 수 있게 한다.

시방서마다 제목 형식이 다르다 (실제 3개 공고에서 확인한 형식):
  - '제 6장. 방 수 공 사' / '제 1 절 일반공통사항'        → 장
  - '제1편 일반시방서' 아래 ' 2. 콘 크 리 트 공 사'         → 편 + (번호형) 장
  - '1.' 다음 줄에 '일반사항' (번호와 제목이 다른 줄)        → 줄 합치기
문서에 '제N장/절'이 있으면 그것을 장으로, 없으면 공종명으로 끝나는 'N. 제목'을 장으로 본다.
"""

from __future__ import annotations

import re

from . import compact

RE_PART = re.compile(r"^\s*제\s*(\d+)\s*편\s*[.\s]?\s*(.*)$")
RE_CHAPTER = re.compile(r"^\s*제\s*(\d+)\s*[장절]\s*[.\s]?\s*(.*)$")
RE_NUM = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.*)$")
RE_LONE_NUM = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s*$")
CHAPTER_SUFFIX = ("공사", "총칙", "일반사항", "공사개요", "시방서", "공통사항")


def _is_toc(lines: list[str]) -> bool:
    head = compact("".join(lines[:6]))
    return "목차" in head or head.startswith("차례")


def _merge_lone_numbers(lines: list[str]) -> list[str]:
    """'1.' / '일반사항' 처럼 나뉜 줄을 '1. 일반사항' 으로 합친다."""
    out, i = [], 0
    while i < len(lines):
        m = RE_LONE_NUM.match(lines[i])
        if m and i + 1 < len(lines) and lines[i + 1].strip() and not RE_LONE_NUM.match(lines[i + 1]):
            out.append(f"{m.group(1)}. {lines[i + 1].strip()}")
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return out


def _looks_like_chapter_title(title: str) -> bool:
    c = compact(title)
    spaced = len(re.findall(r"(?:^|\s)\S(?=\s)", title)) >= 2  # '방 수 공 사' 처럼 띄어 쓴 제목
    return 0 < len(c) <= 20 and (c.endswith(CHAPTER_SUFFIX) or spaced)


def _is_next_chapter(n: int, title: str, cur: int | None) -> bool:
    """번호형 장은 순번이 1씩 올라갈 때만 인정한다.

    KCS 형식은 '2. 콘크리트공사' 아래에 '1. 일반사항 / 2. 재료 / 3. 시공'이 다시 나오므로,
    순번 확인이 없으면 하위 항목이 장으로 잘못 잡힌다. 특기시방서처럼 번호가 1부터
    다시 시작하는 경우는 '…공사'로 끝나는 제목일 때만 허용한다.
    """
    if not _looks_like_chapter_title(title):
        return False
    expected = 1 if cur is None else cur + 1
    restart = n == 1 and cur is not None and compact(title).endswith("공사")
    return n == expected or restart


def split_clauses(pages: list[dict]) -> list[dict]:
    page_lines = []
    for pg in pages:
        lines = [l for l in pg["text"].splitlines() if l.strip()]
        if _is_toc(lines):
            continue
        page_lines.append((pg["page"], _merge_lone_numbers(lines)))

    has_explicit = any(RE_CHAPTER.match(l) for _, ls in page_lines for l in ls)

    clauses: list[dict] = []
    state = {"part": "", "chapter_no": None, "chapter": ""}
    cur: dict | None = None

    def flush():
        if cur and (cur["text"].strip() or cur["heading"]):
            cur["text"] = cur["text"].strip()
            clauses.append(cur)

    def new(num: str, heading: str, page: int) -> dict:
        return {**state, "num": num, "heading": heading, "text": "", "page_start": page, "page_end": page}

    for page, lines in page_lines:
        for line in lines:
            m = RE_PART.match(line)
            if m:
                flush()
                state = {"part": compact(m.group(2)), "chapter_no": None, "chapter": ""}
                cur = new("", "", page)
                continue
            m = RE_CHAPTER.match(line)
            if m:
                flush()
                state = {**state, "chapter_no": int(m.group(1)), "chapter": compact(m.group(2))}
                cur = new("", "", page)
                continue
            m = RE_NUM.match(line)
            if m and len(m.group(2)) <= 60:
                num, title = m.group(1), m.group(2).strip()
                if not has_explicit and "." not in num and _is_next_chapter(int(num), title, state["chapter_no"]):
                    flush()
                    state = {**state, "chapter_no": int(num), "chapter": compact(title)}
                    cur = new("", "", page)
                    continue
                flush()
                cur = new(num, title, page)
                continue
            if cur is None:
                cur = new("", "", page)
            cur["text"] += line.strip() + "\n"
            cur["page_end"] = page
    flush()
    for i, c in enumerate(clauses):
        c["clause_id"] = i
    return clauses
