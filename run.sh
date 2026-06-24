#!/bin/bash
# Arranque manual (útil para pruebas sin systemd).
cd "$(dirname "$0")"
source venv/bin/activate
export MODEL_CHANGER_CONFIG="${MODEL_CHANGER_CONFIG:-config.yaml}"
python3 -m uvicorn model_changer.main:app --host 0.0.0.0 --port 8081
