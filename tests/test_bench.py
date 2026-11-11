"""검출률 측정(bench): 수치 심기 규칙과, P1 원본이 있을 때 전체 실행."""

import json

import pytest

from kdoc import RAW, bench


def test_change_value_doubles_the_number_the_rule_reads():
    item = {"name": "우수드레인 방수보수", "spec": "400*400*400(H), 우레탄방수1mm+코킹"}
    bench._change_value(item)
    assert item["spec"] == "400*400*400(H), 우레탄방수2mm+코킹"


def test_change_value_needs_a_value():
    with pytest.raises(ValueError):
        bench._change_value({"name": "수밀코킹제거", "spec": ""})


@pytest.mark.skipif(not (RAW / "P1" / "manifest.json").exists(), reason="python -m kdoc fetch --project P1 필요")
def test_bench_is_reproducible(tmp_path):
    a = bench.run(["P1"], seed=7, per_type=2, out=tmp_path / "a")
    b = bench.run(["P1"], seed=7, per_type=2, out=tmp_path / "b")
    strip = lambda r: [(c["type"], c["expected"], c["detected"]) for c in r["cases"]]  # noqa: E731
    assert strip(a) == strip(b)
    assert a["summary"]["심음"] == len(a["cases"]) > 0
    saved = json.loads((tmp_path / "a" / "results.json").read_text(encoding="utf-8"))
    assert saved["by_type"].keys() == set(bench.TYPES)
    assert (tmp_path / "a" / "results.md").read_text(encoding="utf-8").startswith("# kdoc 검출률")
