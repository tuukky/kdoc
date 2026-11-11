"""n8n 워크플로 JSON 검사 (PLAN_v2 B7): 생성기와 일치, 연결 대상 존재, 비밀값 없음, 재시도·오류 처리 설정."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

N8N = Path(__file__).resolve().parents[1] / "n8n"
spec = importlib.util.spec_from_file_location("n8n_build", N8N / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
GENERATED = build.build()
FILES = sorted(GENERATED)


def load(name):
    return json.loads((N8N / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FILES)
def test_saved_json_matches_generator(name):
    assert (N8N / name).read_text(encoding="utf-8") == GENERATED[name], "python n8n/build.py 로 다시 만드십시오"


@pytest.mark.parametrize("name", FILES)
def test_connections_point_to_existing_nodes(name):
    wf = load(name)
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    for src, conn in wf["connections"].items():
        assert src in names
        for outs in conn["main"]:
            for target in outs:
                assert target["node"] in names
    # 트리거가 아닌 노드는 모두 어딘가에서 입력을 받는다
    targets = {t["node"] for c in wf["connections"].values() for outs in c["main"] for t in outs}
    for n in wf["nodes"]:
        if not re.search(r"Trigger$|trigger$", n["type"]):
            assert n["name"] in targets, f"{name}: '{n['name']}' 에 들어오는 연결이 없음"


@pytest.mark.parametrize("name", FILES)
def test_no_secrets_in_workflow(name):
    text = (N8N / name).read_text(encoding="utf-8")
    assert not re.search(r"sk-[A-Za-z0-9_-]{16,}|serviceKey=|Bearer [A-Za-z0-9]", text)
    assert '"credentials"' not in text      # 자격 증명은 import 뒤 n8n 화면에서 연결


@pytest.mark.parametrize("name", FILES)
def test_api_calls_retry_and_error_workflow(name):
    wf = load(name)
    for n in wf["nodes"]:
        if n["type"] == "n8n-nodes-base.httpRequest" and n["name"] not in ("상태 확인", "업로드 검토", "판정 저장"):
            assert n.get("retryOnFail") and n.get("maxTries", 1) >= 2, f"{name}: {n['name']} 재시도 없음"
    if wf["id"] != build.IDS["w4err"]:
        assert wf["settings"]["errorWorkflow"] == build.IDS["w4err"]


def test_ids_are_unique_and_n8n_shaped():
    ids = [load(n)["id"] for n in FILES]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"[A-Za-z0-9]{16}", i) for i in ids)


def test_w1_rules_match_plan():
    """PLAN_v2 §4.1·§4.4 의 숫자가 워크플로에 그대로 있다."""
    text = (N8N / "w1_g2b_watch.json").read_text(encoding="utf-8")
    for needle in ("PRICE_MAX = 1_000_000_000", "DAILY_MAX = 30", "조070004", "'화장실', '방수', '외벽', '도장', "
                   "'타일', '창호', '단열', '리모델링'", '"batchInterval": 2000', "0 7 * * *"):
        assert needle in text
