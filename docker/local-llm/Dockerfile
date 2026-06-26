FROM ollama/ollama:latest

# Set up environment for lightweight operation
ENV OLLAMA_NONINTERACTIVE=1
ENV OLLAMA_ROOT=/root/.ollama
ENV OLLAMA_NUM_PARALLEL=1
ENV OLLAMA_MAX_LOADED_MODELS=1

# Pre-download Gemma 2B model during build (lightweight version)
# Using gemma:2b instead of gemma (7B) for resource efficiency
RUN mkdir -p /root/.ollama/models && \
    # Start ollama in background and wait for it to be ready
    nohup ollama serve > /dev/null 2>&1 & \
    OLLAMA_PID=$! && \
    sleep 15 && \
    # Pull the lightweight gemma:2b model
    ollama pull gemma:2b || echo "Pull may have failed, continuing..." && \
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
