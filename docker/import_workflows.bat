@echo off
REM PLAN_v2 n8n 워크플로(W1~W5) 가져오기. 먼저 docker\up.bat 으로 컨테이너를 띄운다.
REM   import_workflows.bat           가져오기 + W2·W3·W4(상태 점검·주간 요약) 활성화
REM   import_workflows.bat w1        위 + W1(매일 07:00 나라장터 수집) 활성화 — 운영 시작(PLAN_v2 D1) 때
REM 같은 ID 로 다시 가져오면 덮어쓴다 (워크플로 ID 는 n8n\build.py 에 고정).
chcp 65001 >nul
cd /d "%~dp0\.."
python n8n\build.py || exit /b 1
for %%F in (w1_g2b_watch w2_upload_form w3_judgment_form w4_error_handler w4_health w4_weekly w5_draft_inquiry) do (
  docker exec kdoc-n8n n8n import:workflow --input=/home/node/workflows/%%F.json || exit /b 1
)
for %%W in (kdocW2UploadFrm1 kdocW3JudgeForm1 kdocW4HealthChk1 kdocW4WeeklySum1) do (
  docker exec kdoc-n8n n8n update:workflow --id=%%W --active=true
)
if /I "%1"=="w1" docker exec kdoc-n8n n8n update:workflow --id=kdocW1G2bWatch01 --active=true

echo.
echo 활성화를 반영하려고 n8n 을 다시 시작합니다.
docker restart kdoc-n8n >nul
echo.
echo n8n:        http://localhost:5678
echo 알림함:     http://127.0.0.1:8000/ui/alerts
echo 업로드 폼:  http://localhost:5678/form/deb5a96d-1d48-571e-8cc7-dbf61b89e09b   (W2)
echo 판정 폼:    http://localhost:5678/form/af286666-a390-536a-8181-6563035dbc39   (W3)
echo.
echo W5(질의서 초안): n8n 화면 Credentials 에서 OpenAI 자격 증명을 만들어 W5 의 'OpenAI 초안' 노드에 연결한 뒤,
echo W1·W2 의 '질의서 초안 (W5, 선택)' 노드를 켜십시오.
