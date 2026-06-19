#!/bin/bash
# =============================================================================
# PAAW - Personal AI Assistant that Works
# One-command startup script
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
ORANGE='\033[38;5;208m'
NC='\033[0m' # No Color

echo -e "${ORANGE}"
echo "    ██████╗  █████╗  █████╗ ██╗    ██╗"
echo "    ██╔══██╗██╔══██╗██╔══██╗██║    ██║"
echo "    ██████╔╝███████║███████║██║ █╗ ██║"
echo "    ██╔═══╝ ██╔══██║██╔══██║██║███╗██║"
echo "    ██║     ██║  ██║██║  ██║╚███╔███╔╝"
echo "    ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝ ╚══╝╚══╝"
echo -e "${NC}"
echo "Personal AI Assistant that Works 🐾"
echo ""

# =============================================================================
# Check prerequisites
# =============================================================================
check_prereqs() {
    echo -e "${YELLOW}Checking prerequisites...${NC}"
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}Error: Docker is not installed${NC}"
        echo "Please install Docker: https://docs.docker.com/get-docker/"
        exit 1
    fi
    
    # Check Docker Compose
    if ! docker compose version &> /dev/null; then
        echo -e "${RED}Error: Docker Compose is not installed${NC}"
        echo "Please install Docker Compose: https://docs.docker.com/compose/install/"
        exit 1
    fi
    
    echo -e "${GREEN}✓ Docker and Docker Compose are installed${NC}"
}

# =============================================================================
# Setup environment
# =============================================================================
setup_env() {
    if [ ! -f ".env" ]; then
        echo -e "${YELLOW}Creating .env file...${NC}"
        cp .env.example .env
        echo -e "${GREEN}✓ Created .env from .env.example${NC}"
        echo ""
        echo -e "${YELLOW}⚠️  IMPORTANT: Add your API key to .env${NC}"
        echo "   Edit .env and add one of:"
        echo "   - ANTHROPIC_API_KEY=your-key (recommended)"
        echo "   - OPENAI_API_KEY=your-key"
        echo "   - GROQ_API_KEY=your-key"
        echo ""
        read -p "Press Enter after you've added your API key..."
    fi
    
    # Verify API key exists
    if ! grep -qE "^(ANTHROPIC_API_KEY|OPENAI_API_KEY|GROQ_API_KEY)=.+" .env 2>/dev/null; then
        echo -e "${YELLOW}Note: No API key found in .env${NC}"
        echo "PAAW can still run using the bundled local Ollama fallback service."
        echo "Add OPENAI_API_KEY or ANTHROPIC_API_KEY only if you want cloud provider models."
        echo ""
    fi
}

# =============================================================================
# Setup SearXNG config
# =============================================================================
setup_searxng() {
    if [ ! -d "configs/searxng" ]; then
        echo -e "${YELLOW}Setting up SearXNG config...${NC}"
        mkdir -p configs/searxng
        cat > configs/searxng/settings.yml << 'EOF'
use_default_settings: true
server:
  secret_key: "paaw-searxng-secret-key-change-me"
  bind_address: "0.0.0.0"
  
search:
  safe_search: 0
  autocomplete: ""
  
general:
  debug: false
  instance_name: "PAAW Search"

enabled_plugins:
  - 'Hash plugin'
  - 'Hostname replace'
  - 'Open Access DOI rewrite'
  - 'Vim-like hotkeys'

engines:
  - name: google
    disabled: false
  - name: bing  
    disabled: false
  - name: duckduckgo
    disabled: false
  - name: wikipedia
    disabled: false
EOF
        echo -e "${GREEN}✓ SearXNG config created${NC}"
    fi
}

# =============================================================================
# Start services
# =============================================================================
start_services() {
    echo ""
    echo -e "${YELLOW}Starting PAAW services...${NC}"
    echo "This may take a few minutes on first run (downloading images)..."
    echo ""
    
    docker compose up -d
    
    echo ""
    echo -e "${GREEN}✓ Services started!${NC}"
}

# =============================================================================
# Wait for services to be ready
# =============================================================================
wait_for_ready() {
    echo ""
    echo -e "${YELLOW}Waiting for services to be ready...${NC}"
    
    # Wait for postgres
    echo -n "  PostgreSQL: "
    for i in {1..30}; do
        if docker compose exec -T postgres pg_isready -U paaw -d paaw &>/dev/null; then
            echo -e "${GREEN}ready${NC}"
            break
        fi
        sleep 1
        echo -n "."
    done
    
    # Wait for PAAW
    echo -n "  PAAW API: "
    for i in {1..30}; do
        if curl -s http://localhost:8080/health &>/dev/null; then
            echo -e "${GREEN}ready${NC}"
            break
        fi
        sleep 1
        echo -n "."
    done
    
    # Wait for SearXNG
    echo -n "  SearXNG: "
    for i in {1..30}; do
        if curl -s http://localhost:8888 &>/dev/null; then
            echo -e "${GREEN}ready${NC}"
            break
        fi
        sleep 1
        echo -n "."
    done
}

# =============================================================================
# Show status
# =============================================================================
show_status() {
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  PAAW is running! 🐾${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  🌐 Web UI:     http://localhost:8080"
    echo "  🔍 Search:     http://localhost:8888 (SearXNG)"
    echo "  📊 Health:     http://localhost:8080/health"
    echo ""
    echo "  Commands:"
    echo "    docker compose logs -f paaw    # View PAAW logs"
    echo "    docker compose down            # Stop all services"
    echo "    docker compose restart paaw    # Restart PAAW"
    echo ""
    echo "  CLI (if installed locally):"
    echo "    paaw chat                      # Start chat"
    echo "    paaw jobs list                 # List scheduled jobs"
    echo ""
}

# =============================================================================
# Main
# =============================================================================
main() {
    case "${1:-}" in
        stop)
            echo -e "${YELLOW}Stopping PAAW...${NC}"
            docker compose down
            echo -e "${GREEN}✓ PAAW stopped${NC}"
            exit 0
            ;;
        restart)
            echo -e "${YELLOW}Restarting PAAW...${NC}"
            docker compose restart
            echo -e "${GREEN}✓ PAAW restarted${NC}"
            exit 0
            ;;
        logs)
            docker compose logs -f paaw
            exit 0
            ;;
        status)
            docker compose ps
            exit 0
            ;;
        *)
            check_prereqs
            setup_env
            setup_searxng
            start_services
            wait_for_ready
            show_status
            ;;
    esac
}

main "$@"
