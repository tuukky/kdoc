@echo off
chcp 65001 >nul
rem kdoc 화면 프로그램 EXE 만들기 → dist\kdoc\kdoc.exe (폴더째 배포)
rem Anaconda 파이썬은 Qt 런타임 충돌·EXE 비대 문제가 있어 python.org 파이썬 3.12 의 .venv-app 을 쓴다.
rem API 키는 EXE 에 넣지 않는다. 사용자가 프로그램의 [OpenAI 키 설정]에서 넣으면 윈도우 자격 증명 관리자에 저장된다.
setlocal
cd /d "%~dp0"

if not exist ".venv-app\Scripts\python.exe" (
  echo [1/3] .venv-app 만들기 - python.org 파이썬 3.12 필요: py -3.12
  py -3.12 -m venv .venv-app || goto :fail
  ".venv-app\Scripts\python" -m pip install -q -e ".[app,build]" || goto :fail
)

rem PATH 에 Anaconda(Library\bin)가 있으면 PyInstaller 가 그쪽 icuuc.dll(ICU 73)·ucrtbase.dll 을 묶어 버려
rem Qt 가 "DLL load failed ... 지정된 프로시저를 찾을 수 없습니다" 로 안 뜬다. 빌드 동안만 PATH 를 윈도우 기본으로.
set "PATH=%SystemRoot%\system32;%SystemRoot%;%SystemRoot%\System32\Wbem"

echo [2/3] EXE 빌드 (몇 분 걸림)
".venv-app\Scripts\pyinstaller" --noconfirm --clean --windowed --name kdoc ^
  --paths src --collect-submodules kdoc ^
  --exclude-module tkinter --exclude-module fastapi --exclude-module uvicorn --exclude-module pytest ^
  kdoc_app.py || goto :fail

echo [3/3] 자재 사전·사용법 복사 (EXE 옆에 두어 사용자가 고칠 수 있게)
copy /y "config\materials.txt" "dist\kdoc\materials.txt" >nul || goto :fail
copy /y "docs\사용법.txt" "dist\kdoc\사용법.txt" >nul || goto :fail

echo.
echo 완료: %~dp0dist\kdoc\kdoc.exe
echo 배포할 때는 dist\kdoc 폴더 전체를 압축해서 전달하세요.
exit /b 0

:fail
echo 빌드 실패
exit /b 1
