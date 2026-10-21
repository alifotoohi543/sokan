@echo off
rem Offline simulator for UI work and tests only — never used for the demo.
set LIARA_MODE=mock
set STATE_DB=C:\Hackaton\liara-copilot\data\mock_state.db
set LLM_API_KEY=
set LLM_KEY_FILE=C:\nonexistent
set MONITOR_INTERVAL_S=30
"C:\Hackaton\liara-copilot\.venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir C:\Hackaton\liara-copilot\backend --port 8011 --host 127.0.0.1
