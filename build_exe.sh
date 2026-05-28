#!/bin/bash
# Sestavení Agilax Log Filter do spustitelného souboru
echo "=== Agilax Log Filter — Build ==="
pip install -r requirements.txt
pyinstaller --onefile --windowed \
    --name "AgilaxLogFilter" \
    --add-data "MODEL_Logo_M.png:." \
    agilax_filter.py
echo "=== Hotovo — soubor je v dist/AgilaxLogFilter ==="
