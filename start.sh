#!/usr/bin/env bash
# Deep Researcher - avvio server (Linux/macOS), indipendente dalla sessione AI
cd "$(dirname "$0")"
echo "============================================"
echo "  Deep Researcher - http://127.0.0.1:8766"
echo "  Ctrl+C per fermare il server"
echo "============================================"
.venv/bin/python -m uvicorn app.server.app:create_app --factory --host 127.0.0.1 --port 8766
