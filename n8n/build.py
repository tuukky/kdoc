"""PLAN_v2 n8n 워크플로(W1~W5) JSON 생성기.

    python n8n/build.py          → n8n/w*.json 다시 만들기 (import 는 docker/import_workflows.bat)

JSON 을 손으로 고치면 Code 노드의 자바스크립트 따옴표·줄바꿈이 깨지기 쉬워 여기서 만든다.
tests/test_n8n_workflows.py 가 생성 결과와 저장된 JSON 이 같은지 확인한다.

공통 규칙 (PLAN_v2 §3 Phase B)
- kdoc-api 는 http://kdoc-api:8000 (같은 compose 네트워크). 비밀값은 JSON 에 넣지 않는다.
- HTTP 노드는 연결 오류 때 3회·5초 간격 재시도. 응답 코드는 전체 응답으로 받아 Code/IF 에서 나눈다.
- 각 워크플로가 실패하면 W4-err(오류 처리 워크플로)가 실패 기록·알림을 남긴다.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "http://kdoc-api:8000"
G2B_SEARCH = "https://www.g2b.go.kr/fi/fiu/fiua/UntySrch/srchTotal.do"
USER_AGENT = "kdoc-research/0.2 (+https://github.com/tuukky/kdoc)"
IDS = {
    "w1": "kdocW1G2bWatch01", "w2": "kdocW2UploadFrm1", "w3": "kdocW3JudgeForm1", "w4err": "kdocW4ErrHandle1",
    "w4health": "kdocW4HealthChk1", "w4weekly": "kdocW4WeeklySum1", "w5": "kdocW5Inquiry001",
}
NS = uuid.UUID("6f1c2b1e-4d1a-4c0e-9a51-2a8f0f6b7d10")


class WF:
    def __init__(self, key: str, name: str, error_workflow: bool = True):
        self.key, self.name, self.nodes, self.conn = key, name, [], {}
        self.error_workflow = error_workflow

    def add(self, name, type_, version, pos, params=None, **extra):
        node = {"id": str(uuid.uuid5(NS, f"{self.key}/{name}")), "name": name, "type": type_,
                "typeVersion": version, "position": list(pos), "parameters": params or {}, **extra}
        self.nodes.append(node)
        return name

    def link(self, src, dst, out=0, inp=0):
        outs = self.conn.setdefault(src, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": dst, "type": "main", "index": inp})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def to_json(self) -> dict:
        settings = {"executionOrder": "v1", "saveManualExecutions": True}
        if self.error_workflow:
            settings["errorWorkflow"] = IDS["w4err"]
        return {"id": IDS[self.key], "name": self.name, "active": False, "nodes": self.nodes,
                "connections": self.conn, "settings": settings, "pinData": {}, "tags": []}


# ---------------------------------------------------------------- 노드 도우미
def code(wf, name, pos, js, each=False, **extra):
    params = {"jsCode": js.strip() + "\n"}
    if each:
        params["mode"] = "runOnceForEachItem"
    return wf.add(name, "n8n-nodes-base.code", 2, pos, params, **extra)


def http(wf, name, pos, url, method="GET", body=None, *, timeout=60_000, full=True, retry=True,
         on_error="continueErrorOutput", multipart=None, headers=None, batch_ms=None, **extra):
    opts = {"timeout": timeout}
    if full:
        opts["response"] = {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}}
    if batch_ms:
        opts["batching"] = {"batch": {"batchSize": 1, "batchInterval": batch_ms}}
    params = {"method": method, "url": url, "options": opts}
    if body is not None:
        params |= {"sendBody": True, "specifyBody": "json", "jsonBody": body}
    if multipart:
        params |= {"sendBody": True, "contentType": "multipart-form-data", "bodyParameters": {"parameters": [
            {"parameterType": "formBinaryData", "name": k, "inputDataFieldName": k} for k in multipart]}}
    if headers:
        params |= {"sendHeaders": True,
                   "headerParameters": {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}}
    if retry:
        extra |= {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000}
    if on_error:
        extra["onError"] = on_error
    return wf.add(name, "n8n-nodes-base.httpRequest", 4.2, pos, params, **extra)


def if_true(wf, name, pos, expr):
    return wf.add(name, "n8n-nodes-base.if", 2.2, pos, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{"id": str(uuid.uuid5(NS, f"{wf.key}/{name}/c")), "leftValue": "={{ " + expr + " }}",
                            "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
            "combinator": "and"},
        "options": {}})


def post_alert(wf, name, pos, expr="$json.alert", **extra):
    return http(wf, name, pos, f"{API}/alerts", "POST", "={{ JSON.stringify(" + expr + ") }}",
                full=False, on_error="continueRegularOutput", **extra)


def form_end(wf, name, pos, title, message):
    return wf.add(name, "n8n-nodes-base.form", 1, pos,
                  {"operation": "completion", "completionTitle": title, "completionMessage": message},
                  webhookId=str(uuid.uuid5(NS, f"{wf.key}/{name}/hook")))


# 공통 자바스크립트 조각
JS_HTTP_DETAIL = """
const detail = (r) => r?.body?.detail ?? r?.body ?? r?.error?.message ?? r?.error ?? '응답 없음';
const httpText = (r) => `HTTP ${r?.statusCode ?? '-'}: ${typeof detail(r) === 'string' ? detail(r) : JSON.stringify(detail(r))}`.slice(0, 500);
const won = (n) => n == null ? '없음' : String(n).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',') + '원';
"""


# ================================================================ W1 나라장터 신규 공고 감시
def w1() -> WF:
    wf = WF("w1", "W1 나라장터 신규 공고 감시 → 검토 → 알림")
    wf.add("매일 07:00", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 0),
           {"rule": {"interval": [{"field": "cronExpression", "expression": "0 7 * * *"}]}})
    wf.add("수동 실행", "n8n-nodes-base.manualTrigger", 1, (0, 200))
    code(wf, "조회 조건", (220, 100), r"""
// 검색 기간: 어제~오늘 (게시일). 같은 공고는 kdoc-api 가 'seen' 으로 걸러 다시 처리하지 않는다.
// 지난 날짜로 시험하려면 OVERRIDE 에 { start: '20261110', end: '20261110' } 처럼 넣고 [수동 실행].
const OVERRIDE = null;
const KEYWORDS = ['화장실', '방수', '외벽', '도장', '타일', '창호', '단열', '리모델링'];  // PLAN_v2 §4.1
const ymd = (d) => new Date(d.getTime() + 9 * 3600e3).toISOString().slice(0, 10).replace(/-/g, '');  // KST
const now = new Date();
const start = OVERRIDE?.start ?? ymd(new Date(now.getTime() - 24 * 3600e3));
const end = OVERRIDE?.end ?? ymd(now);
return KEYWORDS.map((keyword) => ({ json: { keyword, start, end, started: now.toISOString() } }));
""")
    http(wf, "나라장터 검색", (440, 100), G2B_SEARCH, "POST",
         "={{ JSON.stringify({ dlSrchParamM: { currentPage: 1, recordCountPerPage: '100', srchSeCd: '', "
         "untySrchSeCd: 'BK', bizNm: $json.keyword, bizNo: '', bizYmdDiv: 'pstgDt', bizYmd: '', "
         "startBizYmd: $json.start, endBizYmd: $json.end, prcmBsneAreaCd: '조070004', frcpYn: 'N', laseYn: 'N', "
         "rsrvYn: 'N', cucUseYn: 'N' } }) }}",
         full=False, on_error=None, batch_ms=2000, timeout=60_000,
         headers={"User-Agent": USER_AGENT, "Referer": "https://www.g2b.go.kr/"},
         notes="키워드 사이 2초 (PLAN_v2 §4.4). 사이트가 응답하지 않으면 3회 재시도 후 실행 실패 → W4-err")
    code(wf, "공고 정리·필터", (660, 100), r"""
// PLAN_v2 §4.1 (사전 등록): 공사 입찰공고 · 제외 공종 · 추정가격 10억 원 이하 · 하루 30건
const EXCLUDE = /(전기|통신|소방|기계설비|조경|토목|포장)\s*공사/;
const PRICE_MAX = 1_000_000_000;
const DAILY_MAX = 30;
const conds = $('조회 조건').all().map((i) => i.json);
const clean = (s) => String(s ?? '').replace(/&lt;\/?b&gt;|<\/?b>/g, '').replace(/&#40;/g, '(').replace(/&#41;/g, ')')
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').trim();
let found = 0;
const byKey = new Map();
const perKeyword = {};
$input.all().forEach((it, i) => {
  const r = it.json;
  if (!r || !Array.isArray(r.dlTotalSrchL) || !r.dlSetParamM) {
    throw new Error('나라장터 검색 응답 구조가 바뀌었습니다 (dlTotalSrchL 없음) — 수집 중단');
  }
  const kw = conds[i]?.keyword ?? '?';
  const total = r.dlSetParamM.totalSize;
  perKeyword[kw] = total > r.dlTotalSrchL.length ? `${total} (앞 ${r.dlTotalSrchL.length}건만 조회)` : total;
  for (const x of r.dlTotalSrchL) {
    found++;
    const key = `${x.bizNo}-${x.bizOrd}`;
    if (!byKey.has(key)) byKey.set(key, { ...x, keyword: kw });
  }
});
const rows = [...byKey.values()].map((x) => ({
  bid_no: x.bizNo, bid_ord: x.bizOrd, name: clean(x.bizNm), org: clean(x.pbancInstUntyGrpNm),
  price: /^\d+$/.test(String(x.pbancSrchItm03 ?? '')) ? Number(x.pbancSrchItm03) : null,
  posted: x.pbancPstgDt ?? '', area: x.prcmBsneAreaNm, kind: x.untySrchSeCd, keyword: x.keyword,
}));
const dropped = { '공사 아님': 0, '제외 공종': 0, '금액 초과': 0 };
const pass = rows.filter((r) => {
  if (r.kind !== 'BK' || r.area !== '공사') { dropped['공사 아님']++; return false; }
  if (EXCLUDE.test(r.name)) { dropped['제외 공종']++; return false; }
  if (r.price !== null && r.price > PRICE_MAX) { dropped['금액 초과']++; return false; }
  return true;
}).sort((a, b) => String(b.posted).localeCompare(String(a.posted)));
// 하루 상한(DAILY_MAX)은 '새로 등록하는 공고' 수에 적용한다 (kdoc-api 가 셈). 이미 본 공고는 세지 않는다.
const todo = pass;
const stats = {
  기간: `${conds[0].start}~${conds[0].end}`, started: conds[0].started, 검색결과: found, 공고수: rows.length,
  키워드별: perKeyword, 제외: dropped, 필터통과: pass.length, 하루상한: DAILY_MAX,
  금액없음: pass.filter((r) => r.price === null).length,
};
if (!todo.length) return [{ json: { empty: true, stats } }];
return todo.map((r) => ({ json: { ...r, revised: r.bid_ord !== '000', daily_limit: DAILY_MAX, stats } }));
""")
    if_true(wf, "처리할 공고 있음?", (880, 100), "$json.empty !== true")
    wf.add("공고 하나씩", "n8n-nodes-base.splitInBatches", 3, (1100, 0), {"batchSize": 1, "options": {}})
    http(wf, "공고 등록", (1320, 80), f"{API}/bids", "POST",
         "={{ JSON.stringify({ bid_no: $json.bid_no, bid_ord: $json.bid_ord, name: $json.name, org: $json.org, "
         "price: $json.price, posted: $json.posted, daily_limit: $json.daily_limit }) }}", timeout=180_000,
         notes="kdoc-api 가 첨부를 1초 간격으로 확인·분류 (§4.2). 이미 본 공고면 seen")
    code(wf, "등록 결과", (1540, 80), JS_HTTP_DETAIL + r"""
const bid = $('공고 하나씩').item.json;
const r = $json;
// 사이트가 막았거나 하루 요청 상한 → 그날 수집 중단 (§4.4). 실행 실패 → W4-err 가 기록·알림
if (r.statusCode === 503) throw new Error(`나라장터 수집 중단: ${httpText(r)}`);
const project = r.body?.project ?? `G-${bid.bid_no}-${bid.bid_ord}`;
if (r.statusCode >= 200 && r.statusCode < 300) {
  const go = r.body.status === 'new' && r.body.ready;
  return { json: { ...bid, project, next: go ? 'fetch' : 'skip', reason: go ? '' : ({ seen: '이미 처리한 공고', deferred: '하루 상한' }[r.body.status] ?? r.body.reason) } };
}
return { json: { ...bid, project, next: 'fail', step: '공고 등록', reason: httpText(r) } };
""", each=True)
    if_true(wf, "검토 가능?", (1760, 0), "$json.next === 'fetch'")
    if_true(wf, "등록 실패?", (1760, 200), "$json.next === 'fail'")
    http(wf, "첨부 받기", (1980, -100), "={{ '" + API + "/bids/' + $json.project + '/fetch' }}", "POST",
         timeout=600_000)
    code(wf, "받기 결과", (2200, -100), JS_HTTP_DETAIL + r"""
const bid = $('등록 결과').item.json;
return { json: { ...bid, next: $json.statusCode === 200 ? 'review' : 'fail', step: '첨부 받기', reason: httpText($json) } };
""", each=True)
    if_true(wf, "받기 성공?", (2420, -100), "$json.next === 'review'")
    http(wf, "검토", (2640, -200), f"{API}/reviews", "POST", "={{ JSON.stringify({ project: $json.project }) }}",
         timeout=900_000)
    code(wf, "검토 결과", (2860, -200), JS_HTTP_DETAIL + r"""
const bid = $('등록 결과').item.json;
const r = $json;
if (r.statusCode !== 201) return { json: { ...bid, next: 'fail', step: '검토', reason: httpText(r) } };
const s = r.body.summary;
const body = [
  `${bid.org} · 추정가격 ${won(bid.price)}${bid.revised ? ' · 정정 공고' : ''} · 게시 ${bid.posted}`,
  `내역서에 없음 ${s['내역서에 없음']} · 시방서에 없음 ${s['시방서에 없음']} · 수치 다름 ${s['수치 다름']} · 수치 판단 불가 ${s['수치 판단 불가']}`,
  `공정 짝 ${s['짝']} · 짝 판단 불가 ${s['판단 불가']} · 처리 ${r.body.seconds}초`,
  `검토 ID ${r.body.review_id} (판정 폼에 이 ID 를 넣으세요)`,
].join('\n');
return { json: { ...bid, next: 'done', review_id: r.body.review_id, title: bid.name,
  alert: { kind: 'review', severity: 'success', title: `[검토 완료] ${bid.name}`, body, link: r.body.report_url, project: bid.project } } };
""", each=True)
    if_true(wf, "검토 성공?", (3080, -200), "$json.next === 'done'")
    post_alert(wf, "알림: 검토 완료", (3300, -300))
    wf.add("질의서 초안 (W5, 선택)", "n8n-nodes-base.executeWorkflow", 1.2, (3520, -420),
           {"workflowId": {"__rl": True, "value": IDS["w5"], "mode": "id"},
            "options": {"waitForSubWorkflow": False}}, disabled=True,
           notes="OpenAI 자격 증명을 W5 에 연결한 뒤 켜십시오 (PLAN_v2 W5)")
    code(wf, "실패 정리", (2640, 260), JS_HTTP_DETAIL + r"""
const bid = $('공고 하나씩').item.json;
const project = `G-${bid.bid_no}-${bid.bid_ord}`;
const reason = $json.reason ?? `연결 오류: ${$json.error?.message ?? JSON.stringify($json).slice(0, 300)}`;
const step = $json.step ?? '연결';
return { json: {
  event: { source: 'n8n-W1', code: `${step} 실패`, message: `${bid.name}: ${reason}`, level: 'error', project },
  alert: { kind: 'failure', severity: 'warn', title: `[검토 실패] ${bid.name}`, body: `${step} 단계: ${reason}`, project },
} };
""", each=True)
    http(wf, "실패 기록", (2860, 260), f"{API}/ops/events", "POST", "={{ JSON.stringify($json.event) }}",
         full=False, on_error="continueRegularOutput")
    post_alert(wf, "알림: 검토 실패", (3080, 260), "$('실패 정리').item.json.alert")
    http(wf, "이번 실행 통계", (1320, -260),
         "={{ '" + API + "/ops/stats?minutes=' + Math.min(1440, Math.ceil((Date.now() - new Date("
         "$('조회 조건').first().json.started).getTime()) / 60000) + 1) }}",
         full=False, executeOnce=True)
    code(wf, "하루 요약", (1540, -260), r"""
const st = $('공고 정리·필터').first().json.stats;
const api = $input.first().json;
const STATUS = { ready: '검토 대기', fetched: '받음', reviewed: '검토 완료', skipped: '검토 못 함', failed: '실패' };
const kv = (o) => Object.entries(o ?? {}).map(([k, v]) => `${STATUS[k] ?? k} ${v}`).join(' · ') || '없음';
const body = [
  `검색 기간 ${st.기간} · 검색 결과 ${st.검색결과}건 (공고 ${st.공고수}건)`,
  `키워드별: ${kv(st.키워드별)}`,
  `제외: ${kv(st.제외)}`,
  `필터 통과 ${st.필터통과}건 (추정가격 없음 ${st.금액없음}) · 새 공고 하루 상한 ${st.하루상한}건`,
  `새로 등록한 공고 ${api.공고.등록}건: ${kv(api.공고.상태별)} · 하루 상한으로 미룸 ${api['수집 정보']?.['하루 상한으로 미룸'] ?? 0}건`,
  `검토 못 한 사유: ${kv(api.공고['검토 못 한 사유'])}`,
  `검토 성공 ${api.검토.성공} · 실패 ${api.검토.실패} · 처리 시간 P50 ${api.검토['처리 시간 P50(초)'] ?? '-'}초`,
  `실패 이벤트: ${(api['실패 이벤트'] ?? []).map((e) => `${e.code} ${e.n}`).join(' · ') || '없음'}`,
].join('\n');
const severity = (api['실패 이벤트'] ?? []).length ? 'warn' : 'info';
return [{ json: { alert: { kind: 'summary', severity, title: `[W1 요약] 나라장터 수집 ${st.기간}`, body } } }];
""")
    post_alert(wf, "알림: 하루 요약", (1760, -260), executeOnce=True)

    wf.link("매일 07:00", "조회 조건")
    wf.link("수동 실행", "조회 조건")
    wf.chain("조회 조건", "나라장터 검색", "공고 정리·필터", "처리할 공고 있음?")
    wf.link("처리할 공고 있음?", "공고 하나씩", 0)
    wf.link("처리할 공고 있음?", "이번 실행 통계", 1)
    wf.link("공고 하나씩", "이번 실행 통계", 0)       # done
    wf.link("공고 하나씩", "공고 등록", 1)            # loop
    wf.link("공고 등록", "등록 결과", 0)
    wf.link("공고 등록", "실패 정리", 1)
    wf.link("등록 결과", "검토 가능?")
    wf.link("검토 가능?", "첨부 받기", 0)
    wf.link("검토 가능?", "등록 실패?", 1)
    wf.link("등록 실패?", "실패 정리", 0)
    wf.link("등록 실패?", "공고 하나씩", 1)
    wf.link("첨부 받기", "받기 결과", 0)
    wf.link("첨부 받기", "실패 정리", 1)
    wf.link("받기 결과", "받기 성공?")
    wf.link("받기 성공?", "검토", 0)
    wf.link("받기 성공?", "실패 정리", 1)
    wf.link("검토", "검토 결과", 0)
    wf.link("검토", "실패 정리", 1)
    wf.link("검토 결과", "검토 성공?")
    wf.link("검토 성공?", "알림: 검토 완료", 0)
    wf.link("검토 성공?", "실패 정리", 1)
    wf.link("알림: 검토 완료", "공고 하나씩")
    wf.link("알림: 검토 완료", "질의서 초안 (W5, 선택)")
    wf.chain("실패 정리", "실패 기록", "알림: 검토 실패", "공고 하나씩")
    wf.chain("이번 실행 통계", "하루 요약", "알림: 하루 요약")
    return wf


# ================================================================ W2 업로드 검토 폼
JS_PICK_FILES = r"""
// 폼이 붙인 파일 이름(바이너리 키)은 버전마다 달라서 확장자로 골라 spec / boq / file 로 맞춘다.
const out = [];
for (const item of $input.all()) {
  const bin = item.binary ?? {};
  const pick = {};
  for (const [key, f] of Object.entries(bin)) {
    const ext = String(f.fileExtension ?? f.fileName?.split('.').pop() ?? '').toLowerCase();
    if (ext === 'pdf' && !pick.spec) pick.spec = f;
    else if ((ext === 'xlsx' || ext === 'xls') && !pick.boq) pick.boq = f;
  }
  out.push({ json: item.json, binary: pick });
}
return out;
"""


def w2() -> WF:
    wf = WF("w2", "W2 업로드 검토 폼")
    wf.add("검토 요청 폼", "n8n-nodes-base.formTrigger", 2.2, (0, 0), {
        "formTitle": "kdoc 설계도서 검토",
        "formDescription": "시방서(PDF)와 내역서(엑셀)를 올리면 두 문서의 누락·수치 불일치 의심 위치를 엑셀로 정리합니다. "
                           "결과는 알림함(http://127.0.0.1:8000/ui/alerts)에서도 받을 수 있습니다. AI 는 쓰지 않습니다.",
        "formFields": {"values": [
            {"fieldLabel": "시방서 (PDF)", "fieldType": "file", "multipleFiles": False, "acceptFileTypes": ".pdf",
             "requiredField": True},
            {"fieldLabel": "내역서 (엑셀)", "fieldType": "file", "multipleFiles": False,
             "acceptFileTypes": ".xls,.xlsx", "requiredField": True},
            {"fieldLabel": "공사명 (선택)", "fieldType": "text"},
        ]},
        "responseMode": "lastNode", "options": {}}, webhookId=str(uuid.uuid5(NS, "w2/form")))
    code(wf, "파일 맞추기", (220, 0), JS_PICK_FILES)
    http(wf, "업로드 검토", (440, 0), f"{API}/reviews/upload", "POST", multipart=["spec", "boq"], timeout=900_000,
         retry=False)
    code(wf, "결과 정리", (660, 0), JS_HTTP_DETAIL + r"""
const title = $('검토 요청 폼').item.json['공사명 (선택)'] || $('검토 요청 폼').item.binary?.spec?.fileName || '업로드 문서';
const r = $json;
if (r.statusCode !== 201) {
  return { json: { ok: false, message: httpText(r), alert: { kind: 'failure', severity: 'warn', title: `[업로드 검토 실패] ${title}`, body: httpText(r) } } };
}
const s = r.body.summary;
const body = [
  `내역서에 없음 ${s['내역서에 없음']} · 시방서에 없음 ${s['시방서에 없음']} · 수치 다름 ${s['수치 다름']} · 수치 판단 불가 ${s['수치 판단 불가']}`,
  `공정 짝 ${s['짝']} · 짝 판단 불가 ${s['판단 불가']} · 처리 ${r.body.seconds}초`,
  `검토 ID ${r.body.review_id}`,
].join('\n');
return { json: { ok: true, review_id: r.body.review_id, title, message: body,
  report: 'http://127.0.0.1:8000' + r.body.report_url,
  alert: { kind: 'review', severity: 'success', title: `[업로드 검토 완료] ${title}`, body, link: r.body.report_url, project: 'UPLOAD' } } };
""", each=True)
    post_alert(wf, "알림 남기기", (880, 0))
    if_true(wf, "성공?", (1100, 0), "$('결과 정리').item.json.ok")
    form_end(wf, "완료 화면", (1320, -100), "검토가 끝났습니다",
             "={{ $('결과 정리').item.json.message }}\n\n결과 엑셀: {{ $('결과 정리').item.json.report }}\n"
             "알림함: http://127.0.0.1:8000/ui/alerts")
    form_end(wf, "실패 화면", (1320, 100), "검토하지 못했습니다",
             "={{ $('결과 정리').item.json.message }}\n\n시방서는 PDF, 내역서는 xls·xlsx 인지 확인하십시오.")
    wf.add("질의서 초안 (W5, 선택)", "n8n-nodes-base.executeWorkflow", 1.2, (1540, -220),
           {"workflowId": {"__rl": True, "value": IDS["w5"], "mode": "id"},
            "options": {"waitForSubWorkflow": False}}, disabled=True)
    wf.chain("검토 요청 폼", "파일 맞추기", "업로드 검토", "결과 정리", "알림 남기기", "성공?")
    wf.link("업로드 검토", "결과 정리", 1)
    wf.link("성공?", "완료 화면", 0)
    wf.link("성공?", "실패 화면", 1)
    wf.link("완료 화면", "질의서 초안 (W5, 선택)")
    return wf


# ================================================================ W3 판정 폼
def w3() -> WF:
    wf = WF("w3", "W3 판정 폼 → 정밀도 집계")
    wf.add("판정 업로드 폼", "n8n-nodes-base.formTrigger", 2.2, (0, 0), {
        "formTitle": "kdoc 판정 올리기",
        "formDescription": "결과 엑셀의 '판정' 열(진짜 문제 / 문제 아님 / 판단 못 함)을 채운 뒤 올리십시오. "
                           "검토 ID 는 알림함의 검토 완료 알림에 있습니다. 같은 항목을 다시 올리면 새 판정으로 바뀝니다.",
        "formFields": {"values": [
            {"fieldLabel": "검토 ID", "fieldType": "text", "requiredField": True,
             "placeholder": "예: 3f2b8c1e-...."},
            {"fieldLabel": "판정한 결과 엑셀", "fieldType": "file", "multipleFiles": False, "acceptFileTypes": ".xlsx",
             "requiredField": True},
        ]},
        "responseMode": "lastNode", "options": {}}, webhookId=str(uuid.uuid5(NS, "w3/form")))
    code(wf, "입력 확인", (220, 0), r"""
const item = $input.first();
const id = String(item.json['검토 ID'] ?? '').trim();
const file = Object.values(item.binary ?? {}).find((f) => String(f.fileName ?? '').toLowerCase().endsWith('.xlsx'));
const ok = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) && !!file;
return [{ json: { id, ok, message: ok ? '' : '검토 ID 형식(UUID) 또는 .xlsx 파일을 확인하십시오.' }, binary: file ? { file } : {} }];
""")
    if_true(wf, "입력 올바름?", (440, 0), "$json.ok")
    http(wf, "판정 저장", (660, -100), "={{ '" + API + "/reviews/' + $json.id + '/judgments' }}", "POST",
         multipart=["file"], timeout=120_000, retry=False)
    code(wf, "결과 정리", (880, -100), JS_HTTP_DETAIL + r"""
const r = $json;
if (r.statusCode !== 200) return { json: { ok: false, message: httpText(r) } };
const b = r.body;
const p = b.precision['전체'];
const pct = p['정밀도'] == null ? '아직 없음' : `${(p['정밀도'] * 100).toFixed(1)}%`;
const message = [
  `${b.project}: ${b.saved}개 판정 저장 (진짜 문제 ${b.counts['진짜 문제']} · 문제 아님 ${b.counts['문제 아님']} · 판단 못 함 ${b.counts['판단 못 함']})`,
  `전체 정밀도 ${pct} — 진짜 문제 ${p['진짜 문제']} / (진짜 문제 + 문제 아님 ${p['문제 아님']}), 판단 못 함 ${p['판단 못 함']}는 제외`,
].join('\n');
return { json: { ok: true, message, alert: { kind: 'judgment', severity: 'info', title: `[판정 저장] ${b.project}`, body: message, project: b.project } } };
""", each=True)
    if_true(wf, "저장 성공?", (1100, -100), "$json.ok")
    post_alert(wf, "알림 남기기", (1320, -200))
    form_end(wf, "완료 화면", (1540, -200), "판정을 저장했습니다", "={{ $('결과 정리').item.json.message }}")
    form_end(wf, "실패 화면", (1320, 100), "판정을 저장하지 못했습니다", "={{ $json.message }}")
    wf.chain("판정 업로드 폼", "입력 확인", "입력 올바름?")
    wf.link("입력 올바름?", "판정 저장", 0)
    wf.link("입력 올바름?", "실패 화면", 1)
    wf.link("판정 저장", "결과 정리", 0)
    wf.link("판정 저장", "결과 정리", 1)
    wf.link("결과 정리", "저장 성공?")
    wf.link("저장 성공?", "알림 남기기", 0)
    wf.link("저장 성공?", "실패 화면", 1)
    wf.link("알림 남기기", "완료 화면")
    return wf


# ================================================================ W4 실패 처리·상태 점검·주간 요약
def w4err() -> WF:
    wf = WF("w4err", "W4-err 오류 처리 (W1~W5 실패 기록·알림)", error_workflow=False)
    wf.add("워크플로 실패", "n8n-nodes-base.errorTrigger", 1, (0, 0))
    code(wf, "오류 정리", (220, 0), r"""
const e = $input.first().json;
const wfName = e.workflow?.name ?? '알 수 없는 워크플로';
const msg = e.execution?.error?.message ?? e.trigger?.error?.message ?? '오류 메시지 없음';
const node = e.execution?.lastNodeExecuted ?? '-';
const body = [`노드: ${node}`, `오류: ${msg}`, `실행: ${e.execution?.url ?? e.execution?.id ?? '-'}`].join('\n');
return [{ json: {
  event: { source: 'n8n', code: `${wfName} 실패`, message: `${node}: ${msg}`.slice(0, 1000), level: 'error',
           data: { execution: e.execution?.id ?? null, mode: e.execution?.mode ?? null } },
  alert: { kind: 'failure', severity: 'error', title: `[실패] ${wfName}`, body },
} }];
""")
    http(wf, "실패 기록", (440, 0), f"{API}/ops/events", "POST", "={{ JSON.stringify($json.event) }}",
         full=False, on_error="continueRegularOutput")
    post_alert(wf, "알림 남기기", (660, 0), "$('오류 정리').first().json.alert")
    wf.chain("워크플로 실패", "오류 정리", "실패 기록", "알림 남기기")
    return wf


def w4health() -> WF:
    wf = WF("w4health", "W4-health kdoc-api 상태 점검 (10분)")
    wf.add("10분마다", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 0),
           {"rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}})
    http(wf, "상태 확인", (220, 0), f"{API}/health", timeout=5000, full=False, retry=False)
    code(wf, "복구 확인", (440, -100), r"""
// 상태는 워크플로 고정 데이터에 둔다 (활성화한 실행에서만 저장됨 — 수동 실행은 시험용).
const s = $getWorkflowStaticData('global');
if (!s.downSince) { s.fails = 0; return []; }
const mins = Math.round((Date.now() - s.downSince) / 60000);
const since = new Date(s.downSince + 9 * 3600e3).toISOString().slice(0, 16).replace('T', ' ');
s.fails = 0;
delete s.downSince;
return [{ json: { alert: { kind: 'failure', severity: 'warn', title: '[복구] kdoc-api 응답 재개',
  body: `${since}(KST)부터 약 ${mins}분 동안 /health 응답이 없었습니다. 10분 간격 점검, 연속 2회 실패부터 장애로 봅니다.\n그동안의 실패는 n8n 실행 기록(W4-health)에 있습니다.` } } }];
""")
    post_alert(wf, "알림 남기기", (660, -100))
    code(wf, "장애 집계", (440, 100), r"""
// API 가 죽으면 알림함에도 쓸 수 없으므로, 연속 2회째에 이 실행을 '실패'로 표시해 n8n 실행 기록에 남긴다.
// 복구되면 '복구 확인'이 장애 시간을 알림함에 남긴다.
const s = $getWorkflowStaticData('global');
s.fails = (s.fails ?? 0) + 1;
if (s.fails === 1) s.firstFail = Date.now();
if (s.fails === 2) {
  s.downSince = s.firstFail;
  return [{ json: { message: 'kdoc-api /health 연속 2회 응답 없음 (장애 시작)' } }];
}
return [];
""")
    wf.add("장애 표시", "n8n-nodes-base.stopAndError", 1, (660, 100), {"errorMessage": "={{ $json.message }}"})
    wf.link("10분마다", "상태 확인")
    wf.link("상태 확인", "복구 확인", 0)
    wf.link("상태 확인", "장애 집계", 1)
    wf.link("복구 확인", "알림 남기기")
    wf.link("장애 집계", "장애 표시")
    return wf


def w4weekly() -> WF:
    wf = WF("w4weekly", "W4-weekly 주간 운영 요약")
    wf.add("월요일 08:00", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 0),
           {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * 1"}]}})
    wf.add("수동 실행", "n8n-nodes-base.manualTrigger", 1, (0, 200))
    http(wf, "주간 통계", (220, 100), f"{API}/ops/stats?days=7", full=False, on_error=None)
    http(wf, "정밀도", (440, 100), f"{API}/metrics/precision", full=False, on_error=None)
    code(wf, "요약 쓰기", (660, 100), r"""
const s = $('주간 통계').first().json;
const p = $input.first().json['전체'];
const STATUS = { ready: '검토 대기', fetched: '받음', reviewed: '검토 완료', skipped: '검토 못 함', failed: '실패' };
const kv = (o) => Object.entries(o ?? {}).map(([k, v]) => `${STATUS[k] ?? k} ${v}`).join(' · ') || '없음';
const lat = s['공고 등록→알림(초)'];
const body = [
  `기간: ${s['시작'].slice(0, 10)} ~ 오늘`,
  `공고 등록 ${s.공고.등록}건: ${kv(s.공고.상태별)}`,
  `검토 못 한 사유: ${kv(s.공고['검토 못 한 사유'])}`,
  `검토 성공 ${s.검토.성공} · 실패 ${s.검토.실패} (${kv(s.검토.출처별)}) · 처리 시간 P50 ${s.검토['처리 시간 P50(초)'] ?? '-'}초 / P90 ${s.검토['처리 시간 P90(초)'] ?? '-'}초`,
  `공고 등록→알림: ${lat.건수}건, P50 ${lat.P50 ?? '-'}초 / P90 ${lat.P90 ?? '-'}초`,
  `실패 이벤트: ${(s['실패 이벤트'] ?? []).map((e) => `${e.code} ${e.n}`).join(' · ') || '없음'}`,
  `나라장터 요청 수: ${kv(s['외부 요청 수(일별)'])}`,
  `판정 누적 ${p['판정 수']}개, 정밀도 ${p['정밀도'] == null ? '아직 없음' : (p['정밀도'] * 100).toFixed(1) + '%'}`,
].join('\n');
return [{ json: { alert: { kind: 'summary', severity: 'info', title: '[주간 요약] kdoc 운영', body } } }];
""")
    post_alert(wf, "알림 남기기", (880, 100))
    wf.link("월요일 08:00", "주간 통계")
    wf.link("수동 실행", "주간 통계")
    wf.chain("주간 통계", "정밀도", "요약 쓰기", "알림 남기기")
    return wf


# ================================================================ W5 설계 질의서 초안 (선택)
def w5() -> WF:
    wf = WF("w5", "W5 설계 질의서 초안 (선택, OpenAI)")
    wf.add("다른 워크플로에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 0), {"inputSource": "passthrough"})
    wf.add("수동 실행", "n8n-nodes-base.manualTrigger", 1, (0, 200))
    wf.add("시험용 검토 ID", "n8n-nodes-base.set", 3.4, (220, 200), {
        "assignments": {"assignments": [
            {"id": "rid", "name": "review_id", "value": "여기에-검토-ID", "type": "string"},
            {"id": "ttl", "name": "title", "value": "시험", "type": "string"}]}, "options": {}})
    http(wf, "의심 항목 상위 10개", (440, 100), "={{ '" + API + "/reviews/' + $json.review_id + '/top?n=10' }}",
         full=False, on_error=None)
    code(wf, "요청 만들기", (660, 100), r"""
// 문서 본문은 보내지 않는다: 자재 이름·판정 종류·위치(쪽·행)만 (README '문서 외부 전송 최소화').
const top = $input.first().json;
if (!top.items?.length) return [];
const title = $('다른 워크플로에서 호출').isExecuted ? $('다른 워크플로에서 호출').first().json.title : $('시험용 검토 ID').first().json.title;
const items = top.items.map((x, i) => ({ 번호: i + 1, 종류: x.kind, 자재: x.material,
  시방서: x.spec_file ? `${x.spec_file} ${x.spec_page ?? ''}쪽` : '', 내역서: x.boq_file ? `${x.boq_file} ${x.boq_loc ?? ''}` : '' }));
const system = [
  '너는 건설 공사 공무 담당자를 돕는다. 시방서·내역서 비교 도구가 찾은 의심 항목으로 설계사에 보낼 "설계 질의서" 초안을 한국어로 쓴다.',
  '규칙: 오류라고 단정하지 말고 확인을 요청하는 문장으로 쓴다. 목록에 없는 수량·금액·공법을 지어내지 않는다.',
  '항목마다 번호, 위치(시방서 쪽·내역서 위치), 질의 내용 1~2문장. 같은 자재의 항목은 묶어도 된다.',
  '마지막 줄: "본 질의는 검토 보조 도구가 찾은 후보이며 담당자 확인 후 발송합니다."',
].join('\n');
return [{ json: { review_id: top.review_id, project: top.project, title, request: {
  model: 'gpt-5-mini',
  messages: [{ role: 'system', content: system }, { role: 'user', content: `공사: ${title}\n의심 항목:\n${JSON.stringify(items, null, 1)}` }],
  max_completion_tokens: 3000,
} } }];
""")
    wf.add("OpenAI 초안", "n8n-nodes-base.httpRequest", 4.2, (880, 100), {
        "method": "POST", "url": "https://api.openai.com/v1/chat/completions",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "openAiApi",
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.request) }}",
        "options": {"timeout": 180_000}}, retryOnFail=True, maxTries=2, waitBetweenTries=10_000,
        notes="자격 증명: n8n Credentials → OpenAI (키는 n8n 에만 저장). 검토 1건당 호출 1회.")
    code(wf, "초안 정리", (1100, 100), r"""
const req = $('요청 만들기').first().json;
const r = $input.first().json;
const text = r.choices?.[0]?.message?.content?.trim() || '(빈 응답)';
const u = r.usage ?? {};
return [{ json: {
  alert: { kind: 'draft', severity: 'info', title: `[질의서 초안] ${req.title}`, body: text, link: `/reviews/${req.review_id}/report`, project: req.project },
  event: { source: 'n8n-W5', code: 'W5 호출', level: 'info', project: req.project,
           message: `토큰 입력 ${u.prompt_tokens ?? '-'} · 출력 ${u.completion_tokens ?? '-'}`, data: { usage: u, model: r.model } },
} }];
""")
    post_alert(wf, "알림 남기기", (1320, 100))
    http(wf, "호출 기록", (1540, 100), f"{API}/ops/events", "POST", "={{ JSON.stringify($('초안 정리').first().json.event) }}",
         full=False, on_error="continueRegularOutput")
    wf.link("다른 워크플로에서 호출", "의심 항목 상위 10개")
    wf.chain("수동 실행", "시험용 검토 ID", "의심 항목 상위 10개", "요청 만들기", "OpenAI 초안", "초안 정리",
             "알림 남기기", "호출 기록")
    return wf


BUILDERS = {"w1_g2b_watch": w1, "w2_upload_form": w2, "w3_judgment_form": w3, "w4_error_handler": w4err,
            "w4_health": w4health, "w4_weekly": w4weekly, "w5_draft_inquiry": w5}


def build() -> dict[str, str]:
    return {f"{name}.json": json.dumps(fn().to_json(), ensure_ascii=False, indent=2) + "\n"
            for name, fn in BUILDERS.items()}


if __name__ == "__main__":
    for fname, text in build().items():
        (HERE / fname).write_text(text, encoding="utf-8")
        print("wrote", fname)
