@echo off
REM kdoc 자동화 컨테이너(n8n + kdoc-api) 빌드·실행
REM 경로에 한글이 있으면 compose 의 bake 빌드가 실패하므로 끈다
REM ("header key x-docker-expose-session-sharedkey contains non-printable ASCII").
cd /d "%~dp0"
set COMPOSE_BAKE=false
docker compose up -d --build
docker compose ps
echo.
echo n8n:      http://localhost:5678
echo kdoc-api: http://localhost:8000/docs
