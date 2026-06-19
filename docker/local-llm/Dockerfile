FROM ollama/ollama:latest

# Non-interactive mode for builds
ENV OLLAMA_NONINTERACTIVE=1

# Pre-pull the default model at build time so the container is ready-to-serve
RUN ollama pull llama3.2 || true

# Expose the Ollama server port
EXPOSE 11434

# Serve the model on container start. The base image defines an ENTRYPOINT of
# the `ollama` binary, so provide only the subcommand arguments here to avoid
# invoking `ollama` twice (which leads to "unknown command 'ollama' for 'ollama'").
# Run the Ollama serve subcommand with defaults (avoid unsupported flags).
# The Ollama image's ENTRYPOINT is the `ollama` binary, so we only provide
# the `serve` subcommand here. The server typically listens on port 11434.
CMD ["serve"]
