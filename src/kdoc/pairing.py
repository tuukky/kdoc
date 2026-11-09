"""공정 짝짓기: 시방서 장 ↔ 내역서 공종.

1) 이름(공백 무시)이 같은 것은 규칙으로 바로 짝 — AI를 부르지 않는다.
2) 나머지는 OpenAI 가 판단한다. 보내는 것은 '장·공종 이름'뿐이고 문서 본문은 보내지 않는다.
   결과는 캐시에 저장해 같은 입력이면 다시 부르지 않는다. 한 번 실행에 호출 최대 MAX_CALLS 번.
3) 키가 없거나 AI가 실패하면 나머지는 '판단 불가'로 표시한다 (프로그램은 계속 돈다).

판정: 짝 / 판단 불가 / 없음. 사람이 고치지 않고, 근거와 함께 표시만 한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from . import DATA
from .extract import compact
from .sections import AUTO_NOTE

PAIRED, UNSURE, NONE = "짝", "판단 불가", "없음"
MODEL = os.environ.get("KDOC_OPENAI_MODEL", "gpt-5-mini")
MAX_CALLS = 3
KEYRING_SERVICE, KEYRING_USER = "kdoc", "openai_api_key"

SYSTEM = (
    "너는 한국 건축공사 공무 담당자다. 같은 공사의 시방서 '장'과 내역서 '공종'을 같은 공정끼리 짝지어라.\n"
    "- 이름이 달라도 같은 공정이면 짝이다 (예: 도장공사 ↔ 칠공사, 목공사·수장공사 ↔ 목공사및수장공사).\n"
    "- 한 장이 여러 공종과, 여러 장이 한 공종과 짝일 수 있다.\n"
    "- 상대 문서에 대응하는 공정이 분명히 없으면 '없음', 이름만으로 확신할 수 없으면 '판단 불가'.\n"
    "- 공통사항·일반사항처럼 특정 공정이 아닌 장도 '없음'으로 두고 근거에 그렇게 적어라.\n"
    "- 근거(reason)는 한국어 한 문장. 주어진 id만 쓴다."
)

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["spec", "boq"],
    "properties": {
        "spec": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "boq", "status", "reason"],
            "properties": {"id": {"type": "string"}, "boq": {"type": "array", "items": {"type": "string"}},
                           "status": {"type": "string", "enum": [PAIRED, UNSURE, NONE]},
                           "reason": {"type": "string"}}}},
        "boq": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "status", "reason"],
            "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": [UNSURE, NONE]},
                           "reason": {"type": "string"}}}},
    },
}


def cache_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / "kdoc" / "cache" / "pairing"
    return DATA / "cache" / "pairing"


def load_key() -> str | None:
    """윈도우 자격 증명 관리자(keyring) → 환경변수 OPENAI_API_KEY 순서. EXE 안에는 키가 없다."""
    try:
        import keyring
        k = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if k:
            return k
    except Exception:
        pass
    return os.environ.get("OPENAI_API_KEY") or None


def save_key(key: str | None) -> None:
    import keyring
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass


def spec_key(c: dict) -> tuple:
    return (c["doc_id"], c["chapter_no"], c["chapter"])


def boq_key(s: dict) -> tuple:
    return (s["doc_id"], s["section_code"])


def _ask_ai(client, payload: dict, cache: Path) -> dict:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(f"{MODEL}\n{SYSTEM}\n{body}".encode()).hexdigest()[:24]
    cached = cache / f"{h}.json"
    if cached.exists():
        return {**json.loads(cached.read_text(encoding="utf-8")), "_cached": True}
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": body}],
        response_format={"type": "json_schema", "json_schema": {"name": "pairs", "strict": True, "schema": SCHEMA}},
    )
    data = json.loads(resp.choices[0].message.content)
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def pair(chapters: list[dict], sections: list[dict], api_key: str | None = None,
         client=None, cache: Path | None = None, *, manual=()) -> dict:
    """반환: {"spec": [{chapter, status, boq:[section], reason}], "boq": [{section, status, reason}],
              "pairs": [{"spec_key", "boq_key"}], "ai": {"used", "calls", "cached", "error"}}"""
    s_ids = {f"S{i}": c for i, c in enumerate(chapters, 1)}
    b_ids = {f"B{i}": s for i, s in enumerate(sections, 1)}
    spec_res: dict[str, dict] = {}
    boq_res: dict[str, dict] = {}

    # 사용자 지정이 장에 존재하면 해당 장의 자동 후보를 대체한다.
    warnings = []
    spec_lookup = {(c["doc_id"], str(c["chapter_no"] or ""), c["chapter"]): sid for sid, c in s_ids.items()}
    boq_lookup = {boq_key(s): bid for bid, s in b_ids.items()}
    for row in manual:
        if row["note"] == AUTO_NOTE:
            continue
        sk = (row["spec_doc"], row["spec_chapter_no"], row["spec_chapter"])
        bk = (row["boq_doc"], row["boq_section_code"])
        sid, bid = spec_lookup.get(sk), boq_lookup.get(bk)
        if not sid or not bid or (row["boq_section"] and row["boq_section"] != b_ids[bid]["section"]):
            warnings.append(f"공정 짝 참조 확인 필요: {' / '.join(sk)} → {' / '.join(bk)}")
            continue
        result = spec_res.setdefault(sid, {"status": PAIRED, "boq": [], "reason": "사용자 지정 (CSV)"})
        if bid not in result["boq"]:
            result["boq"].append(bid)

    # 1) 지정하지 않은 장은 이름이 같으면 규칙으로
    by_name: dict[str, list[str]] = {}
    for bid, s in b_ids.items():
        by_name.setdefault(compact(s["section"]), []).append(bid)
    for sid, c in s_ids.items():
        same = by_name.get(compact(c["chapter"]), [])
        if same and sid not in spec_res:
            spec_res[sid] = {"status": PAIRED, "boq": same, "reason": "이름 같음 (규칙)"}

    # 2) 나머지는 AI
    rest_s = [sid for sid in s_ids if sid not in spec_res]
    used_b = {b for r in spec_res.values() for b in r["boq"]}
    rest_b = [bid for bid in b_ids if bid not in used_b]
    ai = {"used": False, "calls": 0, "cached": False, "error": None, "model": MODEL}
    if rest_s or rest_b:
        if client is None and api_key:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, timeout=120)
        if client is None:
            ai["error"] = "OpenAI 키 없음 — 이름이 같은 공정만 짝지었습니다"
        else:
            payload = {
                "판단할_시방서_장": [{"id": sid, "이름": s_ids[sid]["chapter"]} for sid in rest_s],
                "판단할_내역서_공종": [{"id": bid, "이름": b_ids[bid]["section"]} for bid in rest_b],
                "전체_내역서_공종(짝 후보)": [{"id": bid, "이름": s["section"], "경로": s.get("path", "")}
                                       for bid, s in b_ids.items()],
            }
            data = None
            while ai["calls"] < MAX_CALLS and data is None:
                ai["calls"] += 1
                try:
                    data = _ask_ai(client, payload, cache or cache_dir())
                except Exception as e:  # 네트워크·키 오류 등
                    ai["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            if data is not None:
                ai.update(used=True, error=None, cached=bool(data.get("_cached")))
                if ai["cached"]:
                    ai["calls"] -= 1  # 캐시는 호출로 세지 않는다
                for r in data.get("spec", []):
                    if r.get("id") in rest_s:
                        boq = list(dict.fromkeys(b for b in r.get("boq", []) if b in b_ids))
                        status = r.get("status") if r.get("status") in (PAIRED, UNSURE, NONE) else UNSURE
                        if status == PAIRED and not boq:
                            status = UNSURE
                        spec_res[r["id"]] = {"status": status, "boq": boq if status == PAIRED else [],
                                             "reason": f"AI: {r.get('reason', '')}"}
                for r in data.get("boq", []):
                    if r.get("id") in rest_b and r.get("status") in (UNSURE, NONE):
                        boq_res[r["id"]] = {"status": r["status"], "reason": f"AI: {r.get('reason', '')}"}

    fallback = ai["error"] or "AI 응답에 없음"
    for sid in s_ids:
        spec_res.setdefault(sid, {"status": UNSURE, "boq": [], "reason": fallback})

    # 공종 쪽: 어느 장이든 짝으로 가리키면 짝
    linked: dict[str, list[str]] = {}
    for sid, r in spec_res.items():
        for bid in r["boq"]:
            linked.setdefault(bid, []).append(sid)
    for bid in b_ids:
        if bid in linked:
            names = ", ".join(s_ids[s]["chapter"] for s in linked[bid])
            boq_res[bid] = {"status": PAIRED, "reason": f"시방서 [{names}] 과 짝"}
        else:
            boq_res.setdefault(bid, {"status": UNSURE, "reason": fallback})

    return {
        "spec": [{"chapter": s_ids[sid], **{k: v for k, v in spec_res[sid].items() if k != "boq"},
                  "boq": [b_ids[b] for b in spec_res[sid]["boq"]]} for sid in s_ids],
        "boq": [{"section": b_ids[bid], **boq_res[bid]} for bid in b_ids],
        "pairs": [{"spec_key": spec_key(s_ids[sid]), "boq_key": boq_key(b_ids[b])}
                  for sid in s_ids for b in spec_res[sid]["boq"]],
        "ai": ai,
        "warnings": warnings,
    }
