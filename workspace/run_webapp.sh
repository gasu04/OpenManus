#!/bin/bash
# OpenManus Web App - Startup Script
# This script starts the OpenManus web interface

# Get the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "  OpenManus Web App"
echo "=========================================="
echo ""

# Check if the venv exists
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [ -f "$VENV_PYTHON" ]; then
    echo "Using virtual environment: $PROJECT_ROOT/.venv"
    PYTHON="$VENV_PYTHON"
else
    echo "No virtual environment found. Using system Python."
    PYTHON="python3"
fi

# Check if required packages are installed
echo "Checking dependencies..."
$PYTHON -c "import fastapi, uvicorn" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing required packages..."
    $PYTHON -m pip install fastapi uvicorn
fi

# Start the web app
echo ""
echo "Starting OpenManus Web App..."
echo "Open your browser and navigate to: http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""

cd "$SCRIPT_DIR"
$PYTHON webapp.py
