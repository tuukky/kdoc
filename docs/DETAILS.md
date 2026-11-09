# kdoc 상세 문서 (운영·API·n8n·추출 세부)

도면·시방서·내역서를 넣으면 **문서 사이의 누락·불일치 의심 항목**을 근거 페이지와 함께 정리해 주는 도구.
"AI가 오류를 보증"하는 도구가 아니라 **공무·설계·감리 담당자의 검토 시간을 줄이는 보조 도구**다. (사업 관점 계획: [business-plan.md](business-plan.md), 기술 계획: [PLAN_v1.md](PLAN_v1.md))

## 화면 프로그램 (EXE) — 개발을 몰라도 쓰는 방법

```
[시방서 칸] [내역서 칸] [도면 칸]  ← 끌어다 놓기 / 파일 찾기
        [검토 시작]
  → 추출 → 공정 짝(이름 같으면 규칙, 나머지 OpenAI) → 시방서↔내역서 양방향 자재·수치 비교
  → 화면 표 + 결과 엑셀 (더블클릭·링크로 원본 해당 쪽/행 열기)
```

- 만들기: `build_exe.bat` → `dist\kdoc\kdoc.exe` (폴더째 배포, 약 240MB). 사용법: `docs/사용법.txt`
- 개발 중 실행: `.venv-app\Scripts\python kdoc_app.py`
- **`.venv-app`(python.org 파이썬 3.12)을 따로 쓰는 이유**: Anaconda 기반 `.venv` 에선 PySide6(Qt 6.12)가 오래된 MSVC 런타임 때문에 `DLL load failed` 로 안 뜬다. 같은 이유로 빌드 때 PATH 에서 Anaconda 를 빼야 한다 (안 빼면 Anaconda 의 ICU `icuuc.dll` 이 EXE 에 묶여 실행 실패). `build_exe.bat` 이 처리한다.
- OpenAI 키: 프로그램의 [OpenAI 키 설정] → 윈도우 자격 증명 관리자. EXE·결과 파일에는 저장하지 않는다. AI 에는 장·공종 **이름만** 보낸다. 키가 없으면 이름 같은 공정만 짝짓고 나머지는 `판단 불가`.
- 같은 검토를 CLI 로: `python -m kdoc review --spec a.pdf --boq b.xlsx [--drawing c.pdf] [--out 폴더] [--no-ai]` 또는 `--project P1`

## 데이터

나라장터(G2B)에 공개된 소규모 건축 보수공사 입찰공고 3건의 첨부 설계서 (`config/sources.yaml`).

| ID | 공사 | 시방서 | 내역서 | 도면 |
|---|---|---|---|---|
| P1 | 국립국악고 기숙사 화장실·샤워실·세탁실 개선 | 건축 42p · 기계 21p | 건축+기계, 기계 | 건축 29장 · 기계 17장 (벡터, 문자 추출 가능) |
| P2 | 도담중 외 2교 지붕방수 철거 및 외벽 보수 | 87p | 건축 | 41장 (**스캔 이미지 → OCR 필요**) |
| P3 | 현암중 화장실 환경개선 및 드라이비트 안전개선 | 화장실 116p · 드라이비트 41p | 건축 | 없음 |

원본 파일은 저장소에 넣지 않는다. `python -m kdoc fetch` 로 언제든 같은 파일을 다시 받는다 (sha256 기록).

## 실행

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m kdoc fetch      # 공고 첨부파일 수집 → data/raw/{P}/ + manifest.json
.venv\Scripts\python -m kdoc extract    # 추출 → data/interim/{P}/*.jsonl + summary.json
.venv\Scripts\python -m kdoc stats      # 시방서 장 / 내역서 공종 목록
.venv\Scripts\python -m kdoc sections   # 공정목록 엑셀(위치·바로 열기 링크) + 짝 지정 파일 config/pairs/{P}.csv
.venv\Scripts\python -m pytest
```

## 검토 API와 n8n 데모

준비된 공고의 시방서와 내역서를 다시 추출해 비교합니다. API는 AI 키 없이 사용자 지정 CSV와 이름 일치 규칙만 사용합니다. 한 번의 요청으로 검토를 완료하고 실행별 UUID로 보고서를 저장하므로, 같은 프로젝트를 동시에 요청해도 결과가 분리됩니다.

```powershell
python -m pip install -e ".[api]"
python -m kdoc fetch --project P1  # 이미 받은 원본이 있으면 생략
python -m uvicorn kdoc.api:app --host 127.0.0.1 --port 8000
```

다른 PowerShell 창에서 정상 요청과 보고서 다운로드:

```powershell
$base = 'http://127.0.0.1:8000'
$result = Invoke-RestMethod "$base/reviews" -Method Post -ContentType 'application/json' -Body '{"project":"P1"}'
$result | ConvertTo-Json -Depth 6
Invoke-WebRequest "$base$($result.report_url)" -OutFile "kdoc-$($result.review_id).xlsx"
```

- `POST /reviews`: 생성 `201` (공정·수치의 `판단 불가`는 요약에 포함), 미등록 `404` (`P9`로 재현), 등록됐지만 파일 미준비 `409`, 시방서·내역서 추출 실패 또는 잘못된 요청 `422`, 내부 실패 `500`.
- `GET /reviews/{review_id}/report`: Excel 첨부 반환. 없는 UUID는 `404`, 잘못된 UUID 형식은 `422`.
- API 문서: `/docs`. 기존 `GET /health`, `POST /extract`는 유지합니다. 생성한 API 보고서는 `data/review/api/{review_id}/report.xlsx`에 저장됩니다.
- 보고서의 원본 링크는 생성 환경의 절대 경로를 사용합니다. Docker에서 내려받은 보고서는 호스트에서 원본 링크가 바로 열리지 않을 수 있습니다. 표에 적힌 PDF 쪽과 내역서 시트·행은 그대로 확인할 수 있습니다. 호스트에서 링크까지 클릭하려면 로컬 API 또는 CLI로 생성하십시오.

```powershell
# 위 uvicorn을 종료한 뒤, Docker로 API와 n8n을 함께 실행
docker\up.bat
docker exec kdoc-n8n n8n import:workflow --input=/home/node/workflows/kdoc_review.json
```

1. `http://localhost:5678`의 **kdoc 검토 → Excel 다운로드**를 엽니다. 대상 `P1`로 실행하면 `검토 요청` 노드에서 요약과 `report_url`, `Excel 다운로드` 노드의 Binary에서 `report`를 확인할 수 있습니다.
2. 대상 값을 `P9`로 바꾸면 **실패 원인** 노드에 `HTTP 404`가 표시됩니다. 아직 파일을 받지 않은 등록 ID로는 `409`를 확인할 수 있습니다. 연결 오류와 다운로드 실패도 이 노드로 분기합니다.
3. 자료를 새로 모으거나 추출 결과를 별도로 남기려면 기존 `n8n/kdoc_pipeline.json`을 사용합니다. 검토 워크플로는 준비된 원본을 한 번만 추출합니다.

- n8n은 **1.123.20 고정**: 1.123.21부터 공식 이미지가 Hardened 이미지로 바뀌어 apk·python3가 없음 (2026-11-03 확인). 보안 업데이트가 끝난 버전이라 로컬 실습용.
- `docker compose`를 직접 쓸 땐 `COMPOSE_BAKE=false` 필요 (경로의 한글 때문에 bake 빌드 실패).
- 두 서비스는 로컬 주소에만 열립니다. API는 인증과 작업 큐가 없는 데모이므로 외부 서비스로 공개하려면 별도 설계가 필요합니다.

## 현재 단계: 1. 추출 (완료)

| 산출물 | 내용 |
|---|---|
| `pages.jsonl` | PDF 페이지별 텍스트, 이미지 수, `needs_ocr`(스캔 페이지) |
| `spec_clauses.jsonl` | 시방서 조항: 편·장(공종)·조항번호·제목·본문·페이지 범위 |
| `drawing_spans.jsonl` | 도면 문자 줄 + 좌표 + 글자 크기, 표제란 반복 문자는 `boilerplate` |
| `boq_items.jsonl` | 내역서 항목: 공종 계층·품명·규격·단위·수량·비고·엑셀 행 번호 |
| `summary.json` | 문서별 추출 건수·소요 시간·오류 |

추출 결과 (2026-11-03): 시방서 7건 → 조항 656개, 공종 장 인식 7/7건 · 내역서 4건 → 항목 403개 · 도면 P1 2건 문자 추출, P2 41장 OCR 대상 표시. 전체 13개 파일 약 6초.

## 비교 — 공정 짝 + 양방향 자재·수치

`kdoc review` / 화면 프로그램 결과 엑셀 `검토결과_{시각}.xlsx`:

| 시트 | 내용 |
|---|---|
| 요약 | 문서 처리 상태, 비교한 자재·수치 수, 차이 의심, 판단 불가, 공정 짝 경고 |
| 공정 짝 | 시방서 장 ↔ 내역서 공종, 판정 `짝`/`판단 불가`/`없음` + 근거(사용자 지정·규칙·AI) |
| 시방서→내역서 | 내역서에서 못 찾은 자재, 같은 부위·조건에서의 수치 차이, 수치를 대응시키기 어려운 경우 |
| 내역서→시방서 | 공종의 자재가 짝 장에 없음(`시방서에 없음`) |
| 안내 | 기준·한계·면책 |

- 자재: `config/materials.txt` 사전에 있는 것만 (EXE 는 옆의 `materials.txt`). 수치: 숫자+단위(mm·회·겹·MPa, THK·T 포함)만.
- 인식한 부위·조건이 양쪽에서 같고 같은 단위의 값이 양쪽에 하나씩 있을 때 수치를 비교합니다. 예: 벽체 30mm는 양쪽 일치, 천장 50mm와 40mm는 차이 의심. 부위 없이 30mm·50mm와 30mm만 있으면 판단 불가입니다. OCR, 복합 치수의 해석, 설계 적합성 판단은 범위 밖입니다.
- 확인 가능한 원본에는 PDF `#page=N`, 엑셀 `시트!A행` 링크가 붙습니다. 누락 의심의 상대 쪽 링크는 짝 공종·장의 시작 위치, 수치 판단 불가의 링크는 후보 위치입니다.
- P1 방수공사 재현: 시방서 PDF 24쪽의 우레탄 방수는 내역서 방수공종에서 미검출, 내역서의 시멘트 액체방수는 짝 장에서 미검출. 이 사례는 공고 문서에서 재현한 의심 건이며, 전체 정확도 표본으로 볼 수 없습니다.

### 공정 짝 직접 지정 (등록 프로젝트 CLI·API)

`python -m kdoc extract --project P1` 후 `python -m kdoc sections --project P1`로 공정 목록과 CSV를 만듭니다. `python -m kdoc review --project P1 --no-ai`와 검토 API가 CSV를 읽습니다. 화면에서 파일을 직접 골라 비교할 때는 등록 프로젝트 CSV가 적용되지 않습니다.

- `data/review/{P}/공정목록_{P}.xlsx`: 시방서 장(시작 쪽)·내역서 공종(시트·첫 행)과 원본 파일 전체 경로, 클릭하면 그 위치로 여는 링크, 짝 지정 여부. 매번 새로 생성.
- `config/pairs/{P}.csv`: 생성한 파일을 덮어쓰지 않습니다. 원하는 짝을 적고 `note`를 `확정`으로 바꾸면 해당 장에 우선 적용됩니다. 다대다 짝은 여러 줄에 적습니다. UTF-8·CP949를 읽으며, 잘못된 참조는 보고서 요약에서 확인할 수 있습니다. CSV가 없거나 장이 미지정이면 자동 짝짓기를 적용합니다.
- PDF 링크(`#page=N`)는 브라우저(Edge·Chrome)로 열 때 해당 쪽으로 이동.

## 다음 단계

2. 비교 보완 (도면 비교, 사전 밖 자재, 정답 테스트셋으로 기준 검증)
3. 리포트 (검토표 엑셀 + 근거 페이지 + 면책 문구)
4. 자동화 (n8n Webhook/주기 수집 → API → 알림, 실패 재시도·로그)
5. 측정 (불일치를 심은 정답 테스트셋으로 검출률·오탐률, 수작업 대비 검토 시간)
