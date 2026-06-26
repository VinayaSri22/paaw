#!/bin/bash
set -e

echo "Starting Ollama server (lightweight mode)..."

# Start ollama server in background
ollama serve &
SERVER_PID=$!

# Give server time to start
sleep 10

# Check if gemma:2b model is already loaded
if ollama list | grep -q "gemma:2b"; then
  echo "Gemma 2B model already exists, skipping download"
else
  echo "Pulling Gemma 2B model (lightweight, first time only)..."
  timeout 600 ollama pull gemma:2b || true
  echo "Gemma 2B model pulled successfully"
fi

# Keep the server running
wait $SERVER_PID
