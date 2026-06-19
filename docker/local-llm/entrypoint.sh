#!/bin/bash
set -e

echo "Starting Ollama server..."

# Start ollama server in background
ollama serve &
SERVER_PID=$!

# Give server time to start
sleep 10

# Check if gemma model is already loaded
if ollama list | grep -q "gemma"; then
  echo "Gemma model already exists, skipping download"
else
  echo "Pulling Gemma model (first time only)..."
  timeout 600 ollama pull gemma || true
  echo "Gemma model pulled successfully"
fi

# Keep the server running
wait $SERVER_PID
