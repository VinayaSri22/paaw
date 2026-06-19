FROM ollama/ollama:latest

# Set up environment
ENV OLLAMA_NONINTERACTIVE=1
ENV OLLAMA_ROOT=/root/.ollama

# Pre-download Gemma model during build
# We need to ensure it's downloaded to the image layer
RUN mkdir -p /root/.ollama/models && \
    # Start ollama in background and wait for it to be ready
    nohup ollama serve > /dev/null 2>&1 & \
    OLLAMA_PID=$! && \
    sleep 15 && \
    # Pull the gemma model
    ollama pull gemma || echo "Pull may have failed, continuing..." && \
    # Give it time to write files
    sleep 5 && \
    # Kill the ollama process
    kill $OLLAMA_PID 2>/dev/null || true && \
    wait $OLLAMA_PID 2>/dev/null || true && \
    sleep 2

# Verify the model was downloaded
RUN ls -lah /root/.ollama/models/manifests/registry.ollama.ai/library/ || echo "Checking model directory..."

# Expose the Ollama server port
EXPOSE 11434

# Run ollama serve on container start
CMD ["serve"]
