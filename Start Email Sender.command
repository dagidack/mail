#!/bin/bash
cd "$(dirname "$0")"
echo "Installing dependencies (if needed)..."
pip3 install -q -r requirements.txt
echo ""
python3 start.py
