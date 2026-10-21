@echo off
rem Sokan — Liara Copilot. Token: "..\Liara API KEy.txt" or env LIARA_API_TOKEN. LLM key: "..\api key openbox.txt" or env LLM_API_KEY.
cd /d %~dp0
if not exist .venv ( python -m venv .venv && .venv\Scripts\pip install -r requirements.txt )
if not exist frontend\dist ( pushd frontend && npm install && npm run build && popd )
start "" http://127.0.0.1:8010
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8010
