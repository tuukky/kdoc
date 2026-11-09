"""공정 짝짓기: 규칙 우선, AI 응답 검증, 키 없음·오류 시 '판단 불가', 캐시·호출 상한."""

import json
from types import SimpleNamespace

from kdoc import pairing as P

CH = [{"doc_id": "S01", "chapter_no": 1, "chapter": "총칙", "page": 1},
      {"doc_id": "S01", "chapter_no": 6, "chapter": "방 수 공 사", "page": 24},
      {"doc_id": "S01", "chapter_no": 9, "chapter": "도장공사", "page": 30}]
SEC = [{"doc_id": "B01", "section_code": "0106", "section": "방수공사", "path": "", "sheet": "내역", "row": 125},
       {"doc_id": "B01", "section_code": "0110", "section": "칠공사", "path": "", "sheet": "내역", "row": 140},
       {"doc_id": "B01", "section_code": "0111", "section": "창호및유리공사", "path": "", "sheet": "내역", "row": 150}]


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.calls, self.sent = 0, []
        self.answer, self.error = answer, error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        self.sent.append(kw["messages"][1]["content"])
        if self.error:
            raise self.error
        msg = SimpleNamespace(content=json.dumps(self.answer, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


ANSWER = {
    "spec": [{"id": "S1", "boq": [], "status": "없음", "reason": "공통사항"},
             {"id": "S3", "boq": ["B2", "B99"], "status": "짝", "reason": "도장=칠"}],
    "boq": [{"id": "B3", "status": "없음", "reason": "시방서에 창호 장 없음"}],
}


def _status(res):
    return ([(r["chapter"]["chapter"], r["status"]) for r in res["spec"]],
            [(r["section"]["section"], r["status"]) for r in res["boq"]])


def test_without_key_only_same_name_pairs(tmp_path):
    res = P.pair(CH, SEC, api_key=None, cache=tmp_path)
    assert _status(res) == ([("총칙", P.UNSURE), ("방 수 공 사", P.PAIRED), ("도장공사", P.UNSURE)],
                            [("방수공사", P.PAIRED), ("칠공사", P.UNSURE), ("창호및유리공사", P.UNSURE)])
    assert "키 없음" in res["spec"][0]["reason"] and not res["ai"]["used"]
    assert res["pairs"] == [{"spec_key": ("S01", 6, "방 수 공 사"), "boq_key": ("B01", "0106")}]


def test_ai_answer_is_validated_and_only_names_are_sent(tmp_path):
    client = FakeClient(ANSWER)
    res = P.pair(CH, SEC, client=client, cache=tmp_path)
    assert _status(res) == ([("총칙", P.NONE), ("방 수 공 사", P.PAIRED), ("도장공사", P.PAIRED)],
                            [("방수공사", P.PAIRED), ("칠공사", P.PAIRED), ("창호및유리공사", P.NONE)])
    assert [s["section"] for s in res["spec"][2]["boq"]] == ["칠공사"]  # 없는 id B99 는 버림
    sent = json.loads(client.sent[0])
    assert [x["이름"] for x in sent["판단할_시방서_장"]] == ["총칙", "도장공사"]  # 규칙으로 정한 것은 안 보냄
    assert "page" not in client.sent[0] and "text" not in client.sent[0]


def test_cache_avoids_second_call(tmp_path):
    client = FakeClient(ANSWER)
    P.pair(CH, SEC, client=client, cache=tmp_path)
    res = P.pair(CH, SEC, client=client, cache=tmp_path)
    assert client.calls == 1 and res["ai"]["cached"] and res["ai"]["calls"] == 0


def test_ai_error_retries_up_to_cap_then_unsure(tmp_path):
    client = FakeClient(error=TimeoutError("network"))
    res = P.pair(CH, SEC, client=client, cache=tmp_path)
    assert client.calls == P.MAX_CALLS
    assert res["spec"][2]["status"] == P.UNSURE and "TimeoutError" in res["spec"][2]["reason"]
    assert res["spec"][1]["status"] == P.PAIRED  # 규칙 짝은 그대로
