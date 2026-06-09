"""
PAAW API Server - V3 Simplified

FastAPI application with WebSocket chat support and HTMX-powered UI.

V3 Architecture:
- No orchestrator, attention manager, or queue
- Direct agent with tool calling loop
- Simple mental model graph with conversations as first-class nodes
"""

import asyncio
import html
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime as dt, timezone
from pathlib import Path
from typing import Optional

import structlog
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from paaw import __version__
from paaw.agent import Agent
from paaw.config import settings
from paaw.models import Channel, UnifiedMessage

logger = structlog.get_logger()

# Store active WebSocket connections
active_connections: dict[str, WebSocket] = {}

# Graph DB instance
_graph_db = None

# Jinja2 templates
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Persistent agent for chat session (maintains conversation context)
_chat_agent: Agent | None = None


async def get_graph_db():
    """Get or create graph DB instance."""
    global _graph_db
    if _graph_db is None:
        from paaw.mental_model import GraphDB
        _graph_db = await GraphDB.create(str(settings.database.url))
    return _graph_db


async def get_or_create_agent() -> Agent:
    """Get or create persistent chat agent."""
    global _chat_agent
    if _chat_agent is None:
        db = await get_graph_db()
        from paaw.mental_model import ContextBuilder
        context_builder = ContextBuilder(db)
        _chat_agent = Agent(graph_db=db, context_builder=context_builder)
        await _chat_agent.initialize()
        logger.info("Created persistent chat agent")
    return _chat_agent


async def init_paaw_node():
    """Initialize PAAW Assistant node in the mental model."""
    from paaw.mental_model.models import NodeType, EdgeType
    
    db = await get_graph_db()
    
    # Check if User node exists
    user_exists = await db.user_exists("user_default")
    if not user_exists:
        logger.warning("No User node found - run onboarding first")
        return None
    
    # Check if PAAW node already exists
    paaw_node = await db.get_node("assistant_paaw")
    if paaw_node:
        logger.info("PAAW node already exists")
    else:
        await db.create_node(
            id="assistant_paaw",
            node_type=NodeType.ASSISTANT,
            label="PAAW",
            context="I am PAAW, your personal AI assistant.",
            key_facts=[
                "Personal AI Assistant that Works",
                "Runs on your machine",
                "Uses MCP tools to help you",
            ],
            attributes={
                "version": __version__,
                "status": "active",
            }
        )
        logger.info("Created PAAW node")
    
    # Ensure edge exists
    try:
        edge_check = await db._cypher(
            "MATCH (u {id: 'user_default'})-[r:HAS_ASSISTANT]->(p {id: 'assistant_paaw'}) RETURN r"
        )
        edge_exists = len(edge_check) > 0
    except Exception:
        edge_exists = False
    
    if not edge_exists:
        await db.create_edge(
            from_id="user_default",
            to_id="assistant_paaw",
            edge_type=EdgeType.HAS_ASSISTANT,
            context="PAAW is the user's personal AI assistant"
        )
        logger.info("Created HAS_ASSISTANT edge")
    
    return await db.get_node("assistant_paaw")


# Background task for periodic cleanup
_cleanup_task: asyncio.Task | None = None


async def periodic_sync_cleanup():
    """Run sync_capabilities once a day to cleanup orphan jobs."""
    while True:
        # Wait 24 hours
        await asyncio.sleep(24 * 60 * 60)  # 24 hours
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            counts = await sync_capabilities(db)
            logger.info("Daily capabilities sync completed", **counts)
        except Exception as e:
            logger.error("Daily sync failed", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _cleanup_task
    logger.info("PAAW API V3 starting up")
    
    try:
        await init_paaw_node()
    except Exception as e:
        logger.error("Failed to initialize PAAW node", error=str(e))
    
    # Sync capabilities (skills, jobs, MCPs) from disk to graph
    try:
        from paaw.mental_model.sync import sync_capabilities
        db = await get_graph_db()
        counts = await sync_capabilities(db)
        logger.info("Capabilities synced on startup", **counts)
    except Exception as e:
        logger.error("Failed to sync capabilities", error=str(e))
    
    # Start daily cleanup background task
    _cleanup_task = asyncio.create_task(periodic_sync_cleanup())
    
    yield
    
    # Cancel cleanup task on shutdown
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    
    logger.info("PAAW API shutting down")


import time


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="PAAW API",
        description="🐾 Personal AI Assistant that Works - V3",
        version=__version__,
        lifespan=lifespan,
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.web.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Add request logging middleware
    @app.middleware("http")
    async def log_requests(request, call_next):
        start_time = time.time()
        
        # Get request details
        client_ip = request.client.host if request.client else "unknown"
        method = request.method
        path = request.url.path
        
        # Skip verbose logging for frequent polling endpoints
        skip_logging = path in ["/htmx/graph/stats", "/api/graph"]
        
        if not skip_logging:
            logger.info(
                "Request started",
                method=method,
                path=path,
                client_ip=client_ip,
            )
        
        response = await call_next(request)
        
        duration_ms = (time.time() - start_time) * 1000
        
        if not skip_logging:
            logger.info(
                "Request completed",
                method=method,
                path=path,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        
        return response

    # Mount static files
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register routes
    register_routes(app)

    return app


def register_routes(app: FastAPI):
    """Register all API routes."""

    # =========================================================================
    # BASIC ENDPOINTS
    # =========================================================================

    @app.get("/")
    async def root():
        return {
            "name": "PAAW",
            "version": __version__,
            "status": "running",
            "message": "🐾 Personal AI Assistant that Works - V3",
        }

    @app.get("/health")
    async def health():
        return JSONResponse(content={
            "status": "healthy",
            "version": __version__,
            "timestamp": dt.utcnow().isoformat(),
        })

    @app.get("/status")
    async def status():
        return {
            "status": "running",
            "version": __version__,
            "config": {
                "model": settings.llm.default_model,
                "debug": settings.debug,
            },
            "connections": len(active_connections),
        }

    # =========================================================================
    # CHAT API
    # =========================================================================

    @app.post("/api/chat")
    async def chat(request: dict):
        """
        Simple chat endpoint for programmatic access.
        
        Accepts:
            message: str - The user's message
            user_id: str (optional) - User identifier (default: settings.default_user_id)
            channel: str (optional) - Channel name (web, discord, slack, etc.)
            metadata: dict (optional) - Additional context
        """
        message = request.get("message", "")
        if not message:
            return {"response": "No message provided"}
        
        # Get optional parameters
        user_id = request.get("user_id", settings.default_user_id)
        channel_name = request.get("channel", "web").lower()
        metadata = request.get("metadata", {})
        
        # Map channel name to Channel enum
        channel_map = {
            "web": Channel.WEB,
            "discord": Channel.CLI,  # Reuse CLI for external channels
            "slack": Channel.CLI,
            "cli": Channel.CLI,
        }
        channel = channel_map.get(channel_name, Channel.WEB)
        
        try:
            agent = await get_or_create_agent()
            unified_msg = UnifiedMessage(
                channel=channel,
                user_id=user_id,
                content=message,
                timestamp=dt.now(timezone.utc),
                metadata=metadata,
            )
            
            response = await agent.process(unified_msg)
            response_text = response.content if hasattr(response, 'content') else str(response)
            
            return {
                "response": response_text,
                "user_id": user_id,
                "channel": channel_name,
            }
        except Exception as e:
            logger.error(f"Chat error: {e}")
            return {"response": f"Error: {str(e)}"}

    # =========================================================================
    # MENTAL MODEL GRAPH API
    # =========================================================================

    @app.get("/api/graph")
    async def get_graph():
        """Get the mental model graph for visualization with expanded sub-nodes and inferred relationships."""
        db = await get_graph_db()
        
        # Get all nodes with more details
        nodes_result = await db._cypher("MATCH (n) RETURN n")
        nodes = []
        virtual_nodes = []  # For conversation messages, etc.
        virtual_edges = []
        
        # Collect domains/projects for semantic matching
        semantic_targets = []  # (id, label, keywords)
        
        for n in nodes_result:
            props = n.get('properties', n)
            
            # Parse key_facts if it's a string
            key_facts = props.get('key_facts', [])
            if isinstance(key_facts, str):
                try:
                    key_facts = json.loads(key_facts)
                except:
                    key_facts = []
            
            node_id = props.get('id', '')
            node_type = props.get('type', 'Unknown')
            node_label = props.get('label', '')
            node_context = props.get('context', '')[:300] if props.get('context') else ''
            
            nodes.append({
                "id": node_id,
                "label": node_label,
                "type": node_type,
                "context": node_context,
                "key_facts": key_facts[:5] if key_facts else [],
            })
            
            # Collect semantic targets (domains, projects, skills)
            if node_type in ['Domain', 'Project', 'Skill']:
                # Build keywords from label and context
                keywords = set()
                for word in (node_label + ' ' + node_context).lower().split():
                    if len(word) > 3:  # Skip short words
                        keywords.add(word.strip('.,!?'))
                semantic_targets.append((node_id, node_label, keywords))
            
            # Expand Conversation nodes to show messages
            if node_type == 'Conversation':
                messages = props.get('messages', [])
                if isinstance(messages, str):
                    try:
                        messages = json.loads(messages)
                    except:
                        messages = []
                
                # Track which topics this conversation discusses
                conv_discusses = set()
                
                # Create virtual nodes for recent messages (max 8)
                for i, msg in enumerate(messages[-8:]):
                    msg_id = f"{node_id}_msg_{i}"
                    role = msg.get('role', 'user')
                    content = msg.get('content', '')
                    content_preview = content[:100]
                    
                    virtual_nodes.append({
                        "id": msg_id,
                        "label": content[:30] + ('...' if len(content) > 30 else ''),
                        "type": "Message",
                        "subtype": role,  # 'user' or 'assistant'
                        "context": content_preview,
                        "key_facts": [],
                        "parent": node_id,
                    })
                    virtual_edges.append({
                        "source": node_id,
                        "type": "CONTAINS",
                        "target": msg_id,
                    })
                    
                    # Check if message discusses any semantic targets
                    content_lower = content.lower()
                    for target_id, target_label, keywords in semantic_targets:
                        # Match if label appears or 2+ keywords match
                        label_match = target_label.lower() in content_lower
                        keyword_matches = sum(1 for kw in keywords if kw in content_lower)
                        
                        if label_match or keyword_matches >= 2:
                            conv_discusses.add(target_id)
                            # Also link message directly to topic
                            virtual_edges.append({
                                "source": msg_id,
                                "type": "MENTIONS",
                                "target": target_id,
                            })
                
                # Create DISCUSSES edges from conversation to topics
                for target_id in conv_discusses:
                    virtual_edges.append({
                        "source": node_id,
                        "type": "DISCUSSES",
                        "target": target_id,
                    })
        
        # Get all edges from graph
        edges = []
        async with db.connection() as conn:
            sql = """
                SELECT * FROM cypher('mental_model', $$ 
                    MATCH (a)-[r]->(b) 
                    RETURN a.id, type(r), b.id 
                $$) AS (source agtype, rel_type agtype, target agtype)
            """
            rows = await conn.fetch(sql)
            for row in rows:
                edges.append({
                    "source": str(row['source']).strip('"'),
                    "type": str(row['rel_type']).strip('"'),
                    "target": str(row['target']).strip('"'),
                })
        
        # Combine real and virtual data
        all_nodes = nodes + virtual_nodes
        all_edges = edges + virtual_edges
        
        return {"nodes": all_nodes, "edges": all_edges}

    @app.get("/api/graph/stats")
    async def get_graph_stats():
        """Get graph statistics."""
        db = await get_graph_db()
        
        # Use direct SQL query for count since Cypher count() has issues
        try:
            async with db.connection() as conn:
                # Count nodes
                nodes_sql = f"""
                    SELECT * FROM cypher('{db.GRAPH_NAME}', $$ 
                        MATCH (n) RETURN count(*) 
                    $$) AS (cnt agtype)
                """
                nodes_result = await conn.fetch(nodes_sql)
                node_count = int(str(nodes_result[0]['cnt'])) if nodes_result else 0
                
                # Count edges
                edges_sql = f"""
                    SELECT * FROM cypher('{db.GRAPH_NAME}', $$ 
                        MATCH ()-[r]->() RETURN count(*) 
                    $$) AS (cnt agtype)
                """
                edges_result = await conn.fetch(edges_sql)
                edge_count = int(str(edges_result[0]['cnt'])) if edges_result else 0
                
            return {"nodes": node_count, "edges": edge_count}
        except Exception as e:
            logger.error(f"Failed to get graph stats: {e}")
            return {"nodes": 0, "edges": 0, "error": str(e)}

    # =========================================================================
    # SERVER ROOM API
    # =========================================================================
    
    @app.get("/api/server-room/stats")
    async def server_room_stats():
        """Get stats for the server room."""
        from paaw.mental_model.sync import load_skills, load_jobs, load_mcp_servers
        
        skills = load_skills()
        jobs = load_jobs()
        mcps = load_mcp_servers()
        
        enabled_mcps = [m for m in mcps if m.get("enabled", False)]
        active_jobs = [j for j in jobs if j.get("status") == "active"]
        
        return HTMLResponse(f"""
            <div class="stat-item">
                <div class="stat-value">{len(enabled_mcps)}</div>
                <div class="stat-label">MCP Servers</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{len(skills)}</div>
                <div class="stat-label">Skills</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{len(active_jobs)}</div>
                <div class="stat-label">Active Jobs</div>
            </div>
        """)
    
    @app.get("/api/server-room/mcps")
    async def server_room_mcps():
        """Get MCP servers list as HTML."""
        from paaw.mental_model.sync import load_mcp_servers
        
        mcps = load_mcp_servers()
        
        if not mcps:
            return HTMLResponse('<div class="p-4 text-center text-gray-500">No MCP servers configured</div>')
        
        html_parts = []
        for mcp in mcps:
            enabled = mcp.get("enabled", False)
            status_class = "online" if enabled else "offline"
            status_text = "Online" if enabled else "Offline"
            status_badge = "status-online" if enabled else "status-offline"
            tools = mcp.get("tools", [])
            
            tools_html = "".join([f'<span class="tool-tag">{t}</span>' for t in tools[:5]])
            if len(tools) > 5:
                tools_html += f'<span class="tool-tag">+{len(tools)-5} more</span>'
            
            html_parts.append(f'''
                <div class="server-unit {status_class}">
                    <div class="server-header" onclick="toggleServer(this)">
                        <div class="server-info">
                            <div class="server-name">{mcp.get("name", "Unknown")}</div>
                            <div class="server-desc">{mcp.get("description", "")[:50]}</div>
                        </div>
                        <span class="status-badge {status_badge}">{status_text}</span>
                        <div class="led-strip">
                            <div class="led {"active" if enabled else ""}"></div>
                            <div class="led"></div>
                            <div class="led"></div>
                        </div>
                    </div>
                    <div class="server-details">
                        <div class="detail-row">
                            <span class="detail-label">Command</span>
                            <span class="detail-value">{mcp.get("command", "")}</span>
                        </div>
                        <div class="tools-list">{tools_html}</div>
                    </div>
                    <div class="server-actions">
                        <button class="action-btn action-btn-secondary" 
                                onclick="testMCPConnection('{mcp.get("name")}', this)"
                                {"disabled" if not enabled else ""}>
                            Test
                        </button>
                        <button class="action-btn action-btn-secondary"
                                onclick="editMCP('{mcp.get("name")}')">
                            Edit
                        </button>
                        <button class="action-btn {"action-btn-danger" if enabled else "action-btn-primary"}" 
                                onclick="toggleMCP('{mcp.get("name")}')">
                            {"Disable" if enabled else "Enable"}
                        </button>
                        <button class="action-btn action-btn-danger"
                                onclick="deleteMCP('{mcp.get("name")}')">
                            Remove
                        </button>
                    </div>
                </div>
            ''')
        
        return HTMLResponse("".join(html_parts))
    
    @app.get("/api/server-room/skills-options")
    async def server_room_skills_options():
        """Get skills as dropdown options for job form."""
        from paaw.mental_model.sync import load_skills
        
        skills = load_skills()
        
        options = ['<option value="">Select a skill...</option>']
        for skill in skills:
            name = skill.get("name", "")
            title = skill.get("title", name)
            options.append(f'<option value="{name}">{title}</option>')
        
        return HTMLResponse("".join(options))
    
    @app.get("/api/server-room/skills-cards")
    async def server_room_skills_cards():
        """Get skills as selectable cards for job wizard."""
        from paaw.mental_model.sync import load_skills
        
        skills = load_skills()
        
        if not skills:
            return HTMLResponse('<div class="p-4 text-center text-zinc-600">No skills available. Create a skill first.</div>')
        
        html_parts = []
        for skill in skills:
            name = skill.get("name", "")
            title = skill.get("title", name)
            persona = skill.get("persona", "")[:80]
            
            html_parts.append(f'''
                <div class="skill-card" data-skill="{name}" onclick="selectSkill('{name}', '{title}')">
                    <div class="skill-card-name">🧠 {title}</div>
                    <div class="skill-card-desc">{persona}...</div>
                </div>
            ''')
        
        return HTMLResponse("".join(html_parts))
    
    @app.get("/api/server-room/tools-list")
    async def server_room_tools_list():
        """Get available MCP tools as checkboxes."""
        from paaw.mental_model.sync import load_mcp_servers
        
        mcps = load_mcp_servers()
        
        html_parts = []
        for mcp in mcps:
            if not mcp.get("enabled"):
                continue
            for tool in mcp.get("tools", []):
                html_parts.append(f'''
                    <div class="tool-checkbox">
                        <input type="checkbox" id="tool-{tool}" name="tools" value="{tool}">
                        <label for="tool-{tool}">{tool}</label>
                        <span style="color: #52525b; font-size: 0.7rem; margin-left: auto;">({mcp.get("name")})</span>
                    </div>
                ''')
        
        if not html_parts:
            return HTMLResponse('<div class="p-4 text-center text-zinc-600">No tools available. Add MCP servers first.</div>')
        
        return HTMLResponse("".join(html_parts))
    
    @app.get("/api/server-room/skills")
    async def server_room_skills():
        """Get skills list as HTML."""
        from paaw.mental_model.sync import load_skills
        
        skills = load_skills()
        
        if not skills:
            return HTMLResponse('<div class="p-4 text-center text-zinc-600">No skills configured</div>')
        
        html_parts = []
        for skill in skills:
            keywords = skill.get("keywords", [])
            keywords_html = "".join([f'<span class="tool-tag">{k}</span>' for k in keywords[:5]])
            
            html_parts.append(f'''
                <div class="server-unit online">
                    <div class="server-header" onclick="toggleServer(this)">
                        <div class="server-info">
                            <div class="server-name">{skill.get("title", skill.get("name", "Unknown"))}</div>
                            <div class="server-desc">{skill.get("persona", "")[:50]}</div>
                        </div>
                        <span class="status-badge status-online">Active</span>
                    </div>
                    <div class="server-details">
                        <div class="detail-row">
                            <span class="detail-label">ID</span>
                            <span class="detail-value">{skill.get("name", "")}</span>
                        </div>
                        <div class="tools-list">{keywords_html}</div>
                    </div>
                    <div class="server-actions">
                        <button class="action-btn action-btn-secondary"
                                onclick="editSkill('{skill.get("name", "")}')">
                            Edit
                        </button>
                        <button class="action-btn action-btn-danger"
                                onclick="deleteSkill('{skill.get("name", "")}')">
                            Delete
                        </button>
                    </div>
                </div>
            ''')
        
        return HTMLResponse("".join(html_parts))

    @app.get("/api/server-room/jobs")
    async def server_room_jobs():
        """Get jobs list as HTML."""
        from paaw.mental_model.sync import load_jobs
        
        jobs = load_jobs()
        
        if not jobs:
            return HTMLResponse('<div class="p-4 text-center text-zinc-600">No jobs configured</div>')
        
        html_parts = []
        for job in jobs:
            status = job.get("status", "active")
            status_class = "online" if status == "active" else "paused" if status == "paused" else "offline"
            status_badge = "status-online" if status == "active" else "status-paused" if status == "paused" else "status-offline"
            
            html_parts.append(f'''
                <div class="server-unit {status_class}">
                    <div class="server-header" onclick="toggleServer(this)">
                        <div class="server-info">
                            <div class="server-name">{job.get("title", job.get("name", "Unknown"))}</div>
                            <div class="server-desc">{job.get("schedule", "")}</div>
                        </div>
                        <span class="status-badge {status_badge}">{status.title()}</span>
                    </div>
                    <div class="server-details">
                        <div class="detail-row">
                            <span class="detail-label">Skill</span>
                            <span class="detail-value">{job.get("skill", "none")}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Goal</span>
                            <span class="detail-value" style="white-space: normal;">{job.get("goal", "")[:100]}</span>
                        </div>
                    </div>
                    <div class="server-actions">
                        <button class="action-btn action-btn-secondary"
                                onclick="runJob('{job.get("name")}')">
                            Run Now
                        </button>
                        <button class="action-btn action-btn-secondary"
                                onclick="editJob('{job.get("name")}')">
                            Edit
                        </button>
                        <button class="action-btn {"action-btn-danger" if status == "active" else "action-btn-primary"}"
                                onclick="toggleJob('{job.get("name")}')">
                            {"Pause" if status == "active" else "Activate"}
                        </button>
                        <button class="action-btn action-btn-danger"
                                onclick="deleteJob('{job.get("name")}')">
                            Delete
                        </button>
                    </div>
                </div>
            ''')
        
        return HTMLResponse("".join(html_parts))
    
    @app.post("/api/server-room/jobs")
    async def create_job(request: Request):
        """Create a new job from form data."""
        form = await request.form()
        name = form.get("name", "").strip()
        goal = form.get("goal", "").strip()
        skill = form.get("skill", "web_researcher")
        schedule = form.get("schedule", "0 9 * * *")
        custom_cron = form.get("custom_cron", "").strip()
        notify_channel = form.get("notify_channel", "").strip()
        
        if not name or not goal:
            return JSONResponse({"error": "Name and goal are required"}, status_code=400)
        
        # Use custom cron if specified
        if schedule == "custom" and custom_cron:
            schedule = custom_cron
        
        # Generate job ID
        import re
        job_id = re.sub(r'[^\w\s-]', '', name.lower())
        job_id = re.sub(r'[-\s]+', '_', job_id)[:50]
        
        # Create job directory and file
        jobs_dir = Path(__file__).parent.parent.parent / "jobs" / job_id
        jobs_dir.mkdir(parents=True, exist_ok=True)
        
        job_content = f'''# {name}

## Meta
created: {dt.now().strftime('%Y-%m-%d')}
created_by: server_room
status: active

## Uses Skill
{skill}

## Goal
{goal}

## What To Find
Based on the goal, find relevant and timely information.

## Delivery
- Format: Clear, concise summary with key points
- Length: Under 2000 characters
- Only alert on: Significant findings related to the goal

## Schedule
cron: {schedule}
timezone: Asia/Kolkata

## How To Notify
{f"Post results to Discord channel ID: {notify_channel}" if notify_channel else "Respond in the chat interface"}

## Context
Created via Server Room UI
'''
        
        job_file = jobs_dir / "job.md"
        job_file.write_text(job_content)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync new job: {e}")
        
        logger.info(f"Created job from Server Room: {job_id}")
        return JSONResponse({"success": True, "job_id": job_id})
    
    @app.post("/api/server-room/jobs/create-with-llm")
    async def create_job_with_llm(request: Request):
        """Create a new job using LLM to generate the job.md content."""
        import re
        import json as json_module
        from paaw.brain.llm import call_llm
        
        data = await request.json()
        name = data.get("name", "").strip()
        goal = data.get("goal", "").strip()
        skill = data.get("skill", "")
        schedule = data.get("schedule", "0 8 * * *")
        notify_type = data.get("notify_type", "chat")
        discord_channel = data.get("discord_channel", "")
        
        if not name or not goal or not skill:
            return JSONResponse({"error": "Name, goal, and skill are required"}, status_code=400)
        
        # Generate job ID
        job_id = re.sub(r'[^\w\s-]', '', name.lower())
        job_id = re.sub(r'[-\s]+', '_', job_id)[:50]
        
        # Load the skill to understand its capabilities
        from paaw.mental_model.sync import load_skills
        skills = load_skills()
        skill_info = next((s for s in skills if s.get("name") == skill), {})
        skill_persona = skill_info.get("persona", "A helpful assistant")
        
        # Determine notification method
        if notify_type == "discord" and discord_channel:
            notify_text = f"Post results to Discord channel ID: {discord_channel}"
        else:
            notify_text = "Respond in the PAAW chat interface"
        
        # Use LLM to generate a well-crafted job.md
        prompt = f"""Generate a job.md configuration file for a scheduled task.

Job Details:
- Name: {name}
- Goal: {goal}
- Skill to use: {skill} ({skill_persona[:100]}...)
- Schedule (cron): {schedule}
- How to notify: {notify_text}

Create a complete job.md file following this exact structure:

```markdown
# [Job Title]

## Meta
created: {dt.now().strftime('%Y-%m-%d')}
created_by: server_room
status: active

## Uses Skill
{skill}

## Goal
[Expand on the user's goal - be specific about what to find/do]

## What To Find
[List specific things to search for or tasks to complete, based on the goal]

## Delivery
- Format: [Specify the format - bullet points, summary, report, etc.]
- Length: [Specify length constraints]
- Only alert on: [What conditions warrant notification]

## Schedule
cron: {schedule}
timezone: Asia/Kolkata

## How To Notify
{notify_text}

## Context
[Add any helpful context about how to execute this job effectively]
```

Generate ONLY the markdown content, nothing else. Make it specific and actionable based on the goal provided."""

        try:
            llm_response = await call_llm(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1500
            )
            
            job_content = llm_response.get("content", "")
            
            # Clean up the response - extract just the markdown
            if "```markdown" in job_content:
                job_content = job_content.split("```markdown")[1].split("```")[0].strip()
            elif "```" in job_content:
                job_content = job_content.split("```")[1].split("```")[0].strip()
            
            # Ensure it starts with a title
            if not job_content.startswith("#"):
                job_content = f"# {name}\n\n{job_content}"
            
        except Exception as e:
            logger.error(f"LLM failed for job generation: {e}")
            # Fallback to basic template
            job_content = f'''# {name}

## Meta
created: {dt.now().strftime('%Y-%m-%d')}
created_by: server_room
status: active

## Uses Skill
{skill}

## Goal
{goal}

## What To Find
Based on the goal above, search for relevant information.

## Delivery
- Format: Clear summary with bullet points
- Length: Under 2000 characters
- Only alert on: Important findings

## Schedule
cron: {schedule}
timezone: Asia/Kolkata

## How To Notify
{notify_text}

## Context
Created via Server Room UI
'''
        
        # Create job directory and file
        jobs_dir = Path(__file__).parent.parent.parent / "jobs" / job_id
        jobs_dir.mkdir(parents=True, exist_ok=True)
        
        job_file = jobs_dir / "job.md"
        job_file.write_text(job_content)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync new job: {e}")
        
        logger.info(f"Created job with LLM from Server Room: {job_id}")
        return JSONResponse({"success": True, "job_id": job_id})
    
    @app.post("/api/server-room/mcps/create-with-llm")
    async def create_mcp_with_llm(request: Request):
        """Create/configure an MCP server using LLM to help with configuration."""
        import json as json_module
        from paaw.brain.llm import call_llm
        
        data = await request.json()
        description = data.get("description", "").strip()
        name = data.get("name", "").strip()
        desc = data.get("desc", "").strip()
        
        if not description:
            return JSONResponse({"error": "Description is required"}, status_code=400)
        
        # Use LLM to figure out the MCP configuration
        prompt = f"""I need to configure an MCP (Model Context Protocol) server for the following capability:

"{description}"

{f"Suggested name: {name}" if name else ""}
{f"Suggested description: {desc}" if desc else ""}

Based on the official MCP servers and common community MCPs, provide the configuration as JSON.

Common MCP servers include:
- GitHub: npx -y @modelcontextprotocol/server-github (needs GITHUB_TOKEN)
- Slack: npx -y @anthropic/mcp-server-slack (needs SLACK_BOT_TOKEN, SLACK_TEAM_ID)
- Filesystem: npx -y @modelcontextprotocol/server-filesystem /path/to/dir
- PostgreSQL: npx -y @modelcontextprotocol/server-postgres postgres://...
- Memory: npx -y @modelcontextprotocol/server-memory
- Puppeteer: npx -y @modelcontextprotocol/server-puppeteer
- Brave Search: npx -y @modelcontextprotocol/server-brave-search (needs BRAVE_API_KEY)
- Google Drive: npx -y @anthropic/mcp-server-gdrive
- Docker-based: docker run -i --rm image-name

Respond with ONLY a JSON object (no markdown, no explanation) with this structure:
{{
    "name": "server-name",
    "description": "What this MCP provides",
    "command": "npx or docker",
    "args": ["array", "of", "arguments"],
    "env": {{"ENV_VAR": "placeholder_value"}},
    "tools": ["tool1", "tool2"],
    "enabled": true,
    "setup_notes": "Any setup instructions for the user"
}}"""

        try:
            llm_response = await call_llm(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800
            )
            
            response_text = llm_response.get("content", "")
            
            # Extract JSON from response
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()
            
            mcp_config = json_module.loads(response_text)
            
        except Exception as e:
            logger.error(f"LLM failed for MCP configuration: {e}")
            # Fallback to basic config
            server_name = name or description.lower().replace(" ", "_")[:20]
            mcp_config = {
                "name": server_name,
                "description": desc or description[:100],
                "command": "npx",
                "args": ["-y", f"@modelcontextprotocol/server-{server_name}"],
                "env": {},
                "tools": [],
                "enabled": False,
                "setup_notes": "Please configure this MCP manually - check https://github.com/modelcontextprotocol/servers"
            }
        
        # Load existing config
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if mcp_file.exists():
            config = json_module.loads(mcp_file.read_text())
        else:
            config = {"mcpServers": {}}
        
        # Add new server
        server_name = mcp_config.pop("name", name or "new_server")
        setup_notes = mcp_config.pop("setup_notes", "")
        
        config["mcpServers"][server_name] = mcp_config
        
        mcp_file.write_text(json_module.dumps(config, indent=2))
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync new MCP: {e}")
        
        logger.info(f"Added MCP server with LLM help: {server_name}")
        return JSONResponse({
            "success": True, 
            "name": server_name,
            "setup_notes": setup_notes
        })
    
    @app.post("/api/server-room/mcps/save")
    async def save_mcp(request: Request):
        """Save MCP server configuration directly (without LLM)."""
        import json as json_module
        
        data = await request.json()
        name = data.get("name", "").strip()
        config = data.get("config", {})
        
        if not name:
            return JSONResponse({"error": "Server name is required"}, status_code=400)
        
        if not config.get("command"):
            return JSONResponse({"error": "Command is required"}, status_code=400)
        
        # Load existing config
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if mcp_file.exists():
            existing = json_module.loads(mcp_file.read_text())
        else:
            existing = {"mcpServers": {}}
        
        # Ensure mcpServers exists
        if "mcpServers" not in existing:
            existing["mcpServers"] = {}
        
        # Add/update server
        existing["mcpServers"][name] = config
        
        mcp_file.write_text(json_module.dumps(existing, indent=2))
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync MCP: {e}")
        
        logger.info(f"Saved MCP server directly: {name}")
        return JSONResponse({"success": True, "name": name})

    @app.post("/api/server-room/mcps/test")
    async def test_mcp_connection(request: Request):
        """Test an MCP server connection by starting it and listing tools."""
        import json as json_module
        from paaw.tools.mcp_client import MCPClient
        
        data = await request.json()
        server_name = data.get("name", "").strip()
        
        if not server_name:
            return JSONResponse({"error": "Server name is required"}, status_code=400)
        
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if not mcp_file.exists():
            return JSONResponse({
                "success": False,
                "error": "MCP config file not found"
            })
        
        config = json_module.loads(mcp_file.read_text())
        server_config = config.get("mcpServers", {}).get(server_name)
        
        if not server_config:
            return JSONResponse({
                "success": False,
                "error": f"Server '{server_name}' not found in configuration"
            })
        
        if not server_config.get("enabled", False):
            return JSONResponse({
                "success": False,
                "error": "Server is disabled. Enable it first to test.",
                "hint": "Set enabled: true in the configuration"
            })
        
        client = MCPClient(mcp_file)
        
        try:
            # Try to start the server
            started = await client.start_server(server_name)
            
            if not started:
                return JSONResponse({
                    "success": False,
                    "error": "Failed to start server",
                    "hint": "Check that the command exists and is accessible"
                })
            
            # Try to list tools
            tools = await client.list_tools(server_name)
            
            # Stop the server after test
            await client.stop_server(server_name)
            
            tool_names = [t.get("name", "unknown") for t in tools]
            
            return JSONResponse({
                "success": True,
                "message": f"Successfully connected to {server_name}!",
                "tools_count": len(tools),
                "tools": tool_names[:10],  # Limit to first 10
                "has_more": len(tools) > 10
            })
            
        except Exception as e:
            logger.error(f"MCP test failed for {server_name}: {e}")
            
            # Try to clean up
            try:
                await client.stop_server(server_name)
            except:
                pass
            
            error_msg = str(e)
            hint = None
            
            # Provide helpful hints based on error
            if "Command not found" in error_msg:
                hint = "The command is not installed or not in PATH"
            elif "Timeout" in error_msg:
                hint = "Server took too long to respond. It may be misconfigured."
            elif "ENOENT" in error_msg or "No such file" in error_msg:
                hint = "Command or file not found"
            
            return JSONResponse({
                "success": False,
                "error": error_msg,
                "hint": hint
            })
        finally:
            await client.stop_all()

    @app.post("/api/server-room/mcps/test-config")
    async def test_mcp_config(request: Request):
        """Test an MCP configuration directly without saving it first."""
        import json as json_module
        import asyncio
        import os
        import shutil
        
        data = await request.json()
        name = data.get("name", "test_server").strip()
        config = data.get("config", {})
        
        if not config.get("command"):
            return JSONResponse({
                "success": False,
                "error": "Command is required in configuration"
            })
        
        command = config.get("command")
        args = config.get("args", [])
        env_vars = config.get("env", {})
        
        # Build environment
        env = os.environ.copy()
        env.update(env_vars)
        
        # Add local bin to PATH for uvx
        local_bin = os.path.expanduser("~/.local/bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        
        # Find the command
        cmd_path = shutil.which(command, path=env.get("PATH"))
        if not cmd_path:
            return JSONResponse({
                "success": False,
                "error": f"Command not found: {command}",
                "hint": f"Make sure '{command}' is installed and in your PATH"
            })
        
        try:
            logger.info(f"Testing MCP config: {name} ({cmd_path} {' '.join(args)})")
            
            process = await asyncio.create_subprocess_exec(
                cmd_path,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            # Send initialize request
            request_id = 1
            init_request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "paaw-test", "version": "0.1.0"}
                }
            }
            
            process.stdin.write((json_module.dumps(init_request) + "\n").encode())
            await process.stdin.drain()
            
            # Read response with timeout
            try:
                while True:
                    response_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=15.0
                    )
                    
                    if not response_line:
                        raise RuntimeError("No response from server")
                    
                    response = json_module.loads(response_line.decode())
                    
                    # Skip notifications
                    if "id" not in response:
                        continue
                    
                    if response.get("id") == request_id:
                        if "error" in response:
                            raise RuntimeError(f"Server error: {response['error']}")
                        break
                
                # Now list tools
                request_id = 2
                tools_request = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": {}
                }
                
                process.stdin.write((json_module.dumps(tools_request) + "\n").encode())
                await process.stdin.drain()
                
                tools = []
                while True:
                    response_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=10.0
                    )
                    
                    if not response_line:
                        break
                    
                    response = json_module.loads(response_line.decode())
                    
                    if "id" not in response:
                        continue
                    
                    if response.get("id") == request_id:
                        tools = response.get("result", {}).get("tools", [])
                        break
                
                tool_names = [t.get("name", "unknown") for t in tools]
                
                return JSONResponse({
                    "success": True,
                    "message": f"✅ Configuration is valid! Server responded correctly.",
                    "tools_count": len(tools),
                    "tools": tool_names[:10],
                    "has_more": len(tools) > 10
                })
                
            except asyncio.TimeoutError:
                return JSONResponse({
                    "success": False,
                    "error": "Server did not respond in time",
                    "hint": "The server may be starting slowly or the configuration may be incorrect"
                })
            finally:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except:
                    process.kill()
                    
        except Exception as e:
            logger.error(f"MCP config test failed: {e}")
            error_msg = str(e)
            hint = None
            
            if "Permission denied" in error_msg:
                hint = "Permission denied - check file permissions"
            elif "No such file" in error_msg:
                hint = "File or directory not found"
            
            return JSONResponse({
                "success": False,
                "error": error_msg,
                "hint": hint
            })

    @app.post("/api/server-room/skills/create-with-llm")
    async def create_skill_with_llm(request: Request):
        """Create a new skill using LLM to generate the skill.md content."""
        import re
        from paaw.brain.llm import call_llm
        
        data = await request.json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        keywords = data.get("keywords", "").strip()
        tools = data.get("tools", [])
        
        if not name or not description:
            return JSONResponse({"error": "Name and description are required"}, status_code=400)
        
        # Generate skill ID
        skill_id = re.sub(r'[^\w\s-]', '', name.lower())
        skill_id = re.sub(r'[-\s]+', '_', skill_id)[:50]
        
        # Format tools for prompt
        tools_text = ", ".join(tools) if tools else "web search, fetch content"
        keywords_text = keywords or name.replace("_", ", ")
        
        prompt = f"""Generate a skill.md configuration file for an AI agent skill.

Skill Details:
- Name: {name}
- Description: {description}
- Available tools: {tools_text}
- Keywords: {keywords_text}

Create a complete skill.md file following this exact structure:

```markdown
# {name.replace('_', ' ').title()}

## Persona
[Write a detailed persona description - who is this skill? How does it behave? What is its expertise?]

## How You Work
[List step-by-step how this skill approaches tasks - be specific and actionable]

## Tools You Use
[List the tools and when to use each one]

## Output Format
[Describe the expected output format - structure, sections, style]

## Keywords
{keywords_text}

## Autonomy
```yaml
can_call_tools: true
can_access_web: true
can_modify_graph: false
max_iterations: 10
timeout_minutes: 30
```
```

Generate ONLY the markdown content. Make it detailed and professional."""

        try:
            llm_response = await call_llm(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1500
            )
            
            skill_content = llm_response.get("content", "")
            
            # Clean up the response
            if "```markdown" in skill_content:
                skill_content = skill_content.split("```markdown")[1].split("```")[0].strip()
            elif skill_content.startswith("```"):
                skill_content = skill_content.split("```")[1].split("```")[0].strip()
            
            # Ensure it starts with a title
            if not skill_content.startswith("#"):
                skill_content = f"# {name.replace('_', ' ').title()}\n\n{skill_content}"
            
        except Exception as e:
            logger.error(f"LLM failed for skill generation: {e}")
            # Fallback template
            skill_content = f'''# {name.replace('_', ' ').title()}

## Persona
{description}

## How You Work
1. Understand the user's request
2. Use available tools to gather information
3. Process and synthesize the results
4. Deliver a clear, helpful response

## Tools You Use
- {tools_text}

## Output Format
Provide clear, structured responses with relevant information.

## Keywords
{keywords_text}

## Autonomy
```yaml
can_call_tools: true
can_access_web: true
can_modify_graph: false
max_iterations: 10
timeout_minutes: 30
```
'''
        
        # Create skill directory and file
        skills_dir = Path(__file__).parent.parent.parent / "skills" / skill_id
        skills_dir.mkdir(parents=True, exist_ok=True)
        
        skill_file = skills_dir / "skill.md"
        skill_file.write_text(skill_content)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync new skill: {e}")
        
        logger.info(f"Created skill with LLM from Server Room: {skill_id}")
        return JSONResponse({"success": True, "skill_id": skill_id})

    @app.post("/api/server-room/jobs/{job_name}/toggle")
    async def toggle_job(job_name: str):
        """Toggle job status between active and paused."""
        job_file = Path(__file__).parent.parent.parent / "jobs" / job_name / "job.md"
        
        if not job_file.exists():
            return JSONResponse({"error": "Job not found"}, status_code=404)
        
        content = job_file.read_text()
        
        if "status: active" in content:
            content = content.replace("status: active", "status: paused")
        elif "status: paused" in content:
            content = content.replace("status: paused", "status: active")
        
        job_file.write_text(content)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync job toggle: {e}")
        
        return JSONResponse({"success": True})
    
    @app.delete("/api/server-room/jobs/{job_name}")
    async def delete_job(job_name: str):
        """Delete a job."""
        import shutil
        job_dir = Path(__file__).parent.parent.parent / "jobs" / job_name
        
        if not job_dir.exists():
            return JSONResponse({"error": "Job not found"}, status_code=404)
        
        shutil.rmtree(job_dir)
        
        # Sync to graph (will remove orphan)
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync job deletion: {e}")
        
        logger.info(f"Deleted job: {job_name}")
        return JSONResponse({"success": True})
    
    @app.get("/api/server-room/mcps/{mcp_name}")
    async def get_mcp(mcp_name: str):
        """Get a single MCP server config for editing."""
        import json as json_module
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if not mcp_file.exists():
            return JSONResponse({"error": "MCP config not found"}, status_code=404)
        
        config = json_module.loads(mcp_file.read_text())
        
        if mcp_name not in config.get("mcpServers", {}):
            return JSONResponse({"error": "MCP server not found"}, status_code=404)
        
        mcp_config = config["mcpServers"][mcp_name]
        mcp_config["name"] = mcp_name
        
        return JSONResponse(mcp_config)
    
    @app.get("/api/server-room/skills/{skill_name}")
    async def get_skill(skill_name: str):
        """Get a single skill config for editing."""
        skill_dir = Path(__file__).parent.parent.parent / "skills" / skill_name
        skill_file = skill_dir / "skill.md"
        
        if not skill_file.exists():
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        
        content = skill_file.read_text()
        
        # Parse the skill.md content
        from paaw.mental_model.sync import parse_skill_md
        skill = parse_skill_md(skill_name, content)
        skill["raw_content"] = content
        
        return JSONResponse(skill)
    
    @app.delete("/api/server-room/skills/{skill_name}")
    async def delete_skill(skill_name: str):
        """Delete a skill."""
        import shutil
        skill_dir = Path(__file__).parent.parent.parent / "skills" / skill_name
        
        if not skill_dir.exists():
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        
        shutil.rmtree(skill_dir)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync skill deletion: {e}")
        
        logger.info(f"Deleted skill: {skill_name}")
        return JSONResponse({"success": True})
    
    @app.put("/api/server-room/skills/{skill_name}")
    async def update_skill(skill_name: str, request: Request):
        """Update an existing skill."""
        data = await request.json()
        content = data.get("content", "")
        
        skill_dir = Path(__file__).parent.parent.parent / "skills" / skill_name
        skill_file = skill_dir / "skill.md"
        
        if not skill_file.exists():
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        
        skill_file.write_text(content)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync skill update: {e}")
        
        logger.info(f"Updated skill: {skill_name}")
        return JSONResponse({"success": True})
    
    @app.get("/api/server-room/jobs/{job_name}")
    async def get_job(job_name: str):
        """Get a single job config for editing."""
        job_dir = Path(__file__).parent.parent.parent / "jobs" / job_name
        job_file = job_dir / "job.md"
        
        if not job_file.exists():
            return JSONResponse({"error": "Job not found"}, status_code=404)
        
        content = job_file.read_text()
        
        # Parse the job.md content
        from paaw.mental_model.sync import parse_job_md
        job = parse_job_md(job_name, content)
        job["raw_content"] = content
        
        return JSONResponse(job)
    
    @app.put("/api/server-room/jobs/{job_name}")
    async def update_job(job_name: str, request: Request):
        """Update an existing job."""
        data = await request.json()
        content = data.get("content", "")
        
        job_dir = Path(__file__).parent.parent.parent / "jobs" / job_name
        job_file = job_dir / "job.md"
        
        if not job_file.exists():
            return JSONResponse({"error": "Job not found"}, status_code=404)
        
        job_file.write_text(content)
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync job update: {e}")
        
        logger.info(f"Updated job: {job_name}")
        return JSONResponse({"success": True})

    @app.post("/api/server-room/jobs/{job_name}/run")
    async def run_job_now(job_name: str):
        """Run a job immediately in the background."""
        from paaw.scheduler.parser import parse_job_md
        from paaw.scheduler.executor import JobExecutor
        
        # Check if job exists
        job_file = Path(__file__).parent.parent.parent / "jobs" / job_name / "job.md"
        if not job_file.exists():
            return JSONResponse({"error": "Job not found"}, status_code=404)
        
        # Parse the job
        job = parse_job_md(job_file)
        if not job:
            return JSONResponse({"error": "Failed to parse job"}, status_code=500)
        
        # Run job in background using asyncio.create_task
        async def run_job_task():
            try:
                logger.info(f"Starting job execution: {job_name}")
                executor = JobExecutor()
                await executor.initialize()
                result = await executor.execute(job)
                logger.info(f"Job {job_name} completed", status=result.status, duration=result.duration_seconds)
                
                # If job has Discord notification, the executor handles it
                if result.should_alert and result.alert_message:
                    logger.info(f"Job {job_name} alert: {result.alert_message[:100]}...")
                    
            except Exception as e:
                logger.error(f"Job {job_name} failed: {e}", exc_info=True)
        
        # Create the background task in the current event loop
        asyncio.create_task(run_job_task())
        
        logger.info(f"Manual job run started: {job_name}")
        return JSONResponse({
            "success": True, 
            "message": f"Job '{job_name}' is now running in the background. Check logs for progress."
        })
    
    @app.post("/api/server-room/mcps/{mcp_name}/toggle")
    async def toggle_mcp(mcp_name: str):
        """Toggle MCP server enabled status."""
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if not mcp_file.exists():
            return JSONResponse({"error": "MCP config not found"}, status_code=404)
        
        import json
        config = json.loads(mcp_file.read_text())
        
        if mcp_name not in config.get("mcpServers", {}):
            return JSONResponse({"error": "MCP server not found"}, status_code=404)
        
        current = config["mcpServers"][mcp_name].get("enabled", False)
        config["mcpServers"][mcp_name]["enabled"] = not current
        
        mcp_file.write_text(json.dumps(config, indent=2))
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync MCP toggle: {e}")
        
        logger.info(f"Toggled MCP {mcp_name}: {not current}")
        return JSONResponse({"success": True, "enabled": not current})
    
    @app.delete("/api/server-room/mcps/{mcp_name}")
    async def delete_mcp(mcp_name: str):
        """Remove an MCP server from config."""
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if not mcp_file.exists():
            return JSONResponse({"error": "MCP config not found"}, status_code=404)
        
        import json
        config = json.loads(mcp_file.read_text())
        
        if mcp_name not in config.get("mcpServers", {}):
            return JSONResponse({"error": "MCP server not found"}, status_code=404)
        
        del config["mcpServers"][mcp_name]
        
        mcp_file.write_text(json.dumps(config, indent=2))
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync MCP deletion: {e}")
        
        logger.info(f"Removed MCP server: {mcp_name}")
        return JSONResponse({"success": True})
    
    @app.post("/api/server-room/mcps")
    async def create_mcp(request: Request):
        """Add a new MCP server."""
        form = await request.form()
        name = form.get("name", "").strip()
        description = form.get("description", "").strip()
        command = form.get("command", "").strip()
        args_text = form.get("args", "").strip()
        env_text = form.get("env", "").strip()
        tools_text = form.get("tools", "").strip()
        
        if not name or not command:
            return JSONResponse({"error": "Name and command are required"}, status_code=400)
        
        # Parse args
        args = [a.strip() for a in args_text.split('\n') if a.strip()] if args_text else []
        
        # Parse env
        env = {}
        if env_text:
            for line in env_text.split('\n'):
                if '=' in line:
                    key, val = line.split('=', 1)
                    env[key.strip()] = val.strip()
        
        # Parse tools
        tools = [t.strip() for t in tools_text.split(',') if t.strip()] if tools_text else []
        
        mcp_file = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        import json
        if mcp_file.exists():
            config = json.loads(mcp_file.read_text())
        else:
            config = {"mcpServers": {}}
        
        config["mcpServers"][name] = {
            "description": description,
            "command": command,
            "args": args,
            "env": env,
            "enabled": True,
            "tools": tools,
        }
        
        mcp_file.write_text(json.dumps(config, indent=2))
        
        # Sync to graph
        try:
            from paaw.mental_model.sync import sync_capabilities
            db = await get_graph_db()
            await sync_capabilities(db)
        except Exception as e:
            logger.error(f"Failed to sync new MCP: {e}")
        
        logger.info(f"Added MCP server: {name}")
        return JSONResponse({"success": True, "name": name})

    # =========================================================================
    # UI PAGES
    # =========================================================================

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Main dashboard page."""
        return templates.TemplateResponse("base_new.html", {
            "request": request,
            "version": __version__,
        })

    @app.get("/server-room", response_class=HTMLResponse)
    async def server_room(request: Request):
        """Server Room - manage MCPs, Skills, and Jobs."""
        return templates.TemplateResponse("pages/server_room.html", {
            "request": request,
            "version": __version__,
        })

    @app.get("/viz", response_class=HTMLResponse)
    async def viz_page(request: Request):
        """Graph visualization page."""
        return templates.TemplateResponse("base_new.html", {
            "request": request,
            "version": __version__,
        })

    # =========================================================================
    # HTMX PARTIALS
    # =========================================================================

    @app.get("/htmx/graph/stats")
    async def htmx_graph_stats(request: Request):
        """HTMX partial for graph stats - clean display."""
        stats = await get_graph_stats()
        return HTMLResponse(f"""
            <span>{stats['nodes']} nodes · {stats['edges']} connections</span>
        """)

    @app.post("/htmx/chat/send")
    async def htmx_chat_send(request: Request, message: str = Form(...)):
        """HTMX endpoint for sending chat messages."""
        start_time = time.time()
        
        if not message.strip():
            logger.warning("Chat request with empty message")
            return HTMLResponse('<div class="error">Please enter a message</div>')
        
        logger.info(
            "Chat message received",
            message_length=len(message),
            message_preview=message[:50] + "..." if len(message) > 50 else message,
        )
        
        try:
            agent = await get_or_create_agent()
            unified_msg = UnifiedMessage(
                channel=Channel.WEB,
                user_id=settings.default_user_id,
                content=message,
                timestamp=dt.now(timezone.utc),
                metadata={},
            )
            
            response = await agent.process(unified_msg)
            response_text = response.content if hasattr(response, 'content') else str(response)
            
            duration_ms = (time.time() - start_time) * 1000
            logger.info(
                "Chat response generated",
                response_length=len(response_text),
                duration_ms=round(duration_ms, 2),
                tools_used=response.tools_used if hasattr(response, 'tools_used') else [],
            )
            
            # Return raw response - frontend will render markdown
            # Escape HTML to prevent XSS but keep markdown intact
            escaped_response = response_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            
            # Return with markdown class for frontend rendering
            return HTMLResponse(f"""
                <div class="flex justify-start mb-4 assistant-message">
                    <div class="flex gap-3 max-w-[85%]">
                        <div class="w-8 h-8 rounded-full bg-gradient-to-br from-amber-400 to-orange-500 flex items-center justify-center flex-shrink-0 text-white text-sm font-bold shadow-md">
                            🐾
                        </div>
                        <div class="flex-1 px-4 py-3 rounded-2xl rounded-tl-sm bg-zinc-100 dark:bg-zinc-800 text-zinc-800 dark:text-zinc-100 border border-zinc-200 dark:border-zinc-700 prose prose-sm dark:prose-invert max-w-none markdown-content" data-markdown="{escaped_response.replace(chr(34), '&quot;')}">
                            <div class="loading-dots flex gap-1">
                                <span class="w-2 h-2 bg-amber-500 rounded-full animate-bounce" style="animation-delay: 0s"></span>
                                <span class="w-2 h-2 bg-amber-500 rounded-full animate-bounce" style="animation-delay: 0.1s"></span>
                                <span class="w-2 h-2 bg-amber-500 rounded-full animate-bounce" style="animation-delay: 0.2s"></span>
                            </div>
                        </div>
                    </div>
                </div>
            """)
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                "Chat error",
                error=str(e),
                error_type=type(e).__name__,
                duration_ms=round(duration_ms, 2),
            )
            return HTMLResponse(f'<div class="text-red-400 p-3">Error: {str(e)}</div>')

    @app.get("/htmx/chat/history")
    async def htmx_chat_history():
        """Get chat history (returns empty for now - history is in-memory on agent)."""
        return HTMLResponse("""
            <div class="text-center py-8 text-void-500">
                <div class="text-3xl mb-2">🐾</div>
                <div>Start a conversation with PAAW!</div>
            </div>
        """)

    @app.post("/htmx/chat/clear")
    async def htmx_chat_clear():
        """Clear chat history."""
        global _chat_agent
        if _chat_agent:
            _chat_agent.conversation_history = []
        return HTMLResponse("""
            <div class="text-center py-8 text-void-500">
                <div class="text-3xl mb-2">🐾</div>
                <div>Chat cleared. Start a new conversation!</div>
            </div>
        """)

    @app.post("/htmx/chat")
    async def htmx_chat(request: Request, message: str = Form(...)):
        """HTMX endpoint for chat messages (legacy)."""
        return await htmx_chat_send(request, message)

    # =========================================================================
    # WEBSOCKET CHAT
    # =========================================================================

    @app.websocket("/ws/chat/{client_id}")
    async def websocket_chat(websocket: WebSocket, client_id: str):
        """WebSocket endpoint for real-time chat."""
        await websocket.accept()
        active_connections[client_id] = websocket
        logger.info(f"WebSocket connected: {client_id}")
        
        try:
            while True:
                data = await websocket.receive_text()
                message_data = json.loads(data)
                message = message_data.get("message", "")
                
                if not message:
                    continue
                
                try:
                    agent = await get_or_create_agent()
                    unified_msg = UnifiedMessage(
                        channel=Channel.WEB,
                        user_id=settings.default_user_id,
                        content=message,
                        timestamp=dt.now(timezone.utc),
                        metadata={},
                    )
                    
                    response = await agent.process(unified_msg)
                    response_text = response.content if hasattr(response, 'content') else str(response)
                    
                    await websocket.send_json({
                        "type": "response",
                        "content": response_text,
                    })
                except Exception as e:
                    logger.error(f"WebSocket chat error: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "content": str(e),
                    })
        except WebSocketDisconnect:
            del active_connections[client_id]
            logger.info(f"WebSocket disconnected: {client_id}")

    # =========================================================================
    # MCP SERVERS API
    # =========================================================================

    @app.get("/api/mcp/servers")
    async def list_mcp_servers():
        """List all configured MCP servers."""
        mcp_config_path = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        try:
            if mcp_config_path.exists():
                with open(mcp_config_path) as f:
                    config = json.load(f)
                
                servers = []
                for name, cfg in config.get("mcpServers", {}).items():
                    servers.append({
                        "name": name,
                        "description": cfg.get("description", ""),
                        "enabled": cfg.get("enabled", False),
                        "tools": cfg.get("tools", []),
                    })
                return {"servers": servers}
            return {"servers": []}
        except Exception as e:
            logger.error(f"Failed to list MCP servers: {e}")
            return {"servers": [], "error": str(e)}

    # =========================================================================
    # ONBOARDING
    # =========================================================================

    @app.get("/onboarding", response_class=HTMLResponse)
    async def onboarding_page(request: Request):
        """Onboarding page."""
        return templates.TemplateResponse("base_new.html", {
            "request": request,
            "version": __version__,
            "page": "onboarding",
        })

    @app.post("/api/onboarding")
    async def run_onboarding(request: dict):
        """Run onboarding flow."""
        try:
            from paaw.onboarding import OnboardingFlow
            
            flow = OnboardingFlow()
            db = await get_graph_db()
            
            # Process user info
            user_data = request.get("user_data", {})
            result = await flow.process_user_info(user_data, db)
            
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"Onboarding error: {e}")
            return {"success": False, "error": str(e)}


# Create app instance
app = create_app()


async def run_server(app: FastAPI = None, shutdown_event: asyncio.Event = None):
    """Run the API server with optional shutdown event."""
    from pathlib import Path
    import uvicorn
    
    if app is None:
        app = create_app()
    
    # Configure uvicorn logging to write to file
    logs_dir = Path(__file__).parent.parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    # Uvicorn log config with rotating file handlers
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "fmt": "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(logs_dir / "uvicorn.log"),
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "default",
                "encoding": "utf-8",
            },
            "access_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(logs_dir / "uvicorn.log"),
                "maxBytes": 10485760,
                "backupCount": 5,
                "formatter": "access",
                "encoding": "utf-8",
            },
            "default_console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "default",
            },
            "access_console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "access",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default_file", "default_console"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default_file", "default_console"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access_file", "access_console"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
    
    config = uvicorn.Config(
        app,
        host=settings.web.host,
        port=settings.web.port,
        log_level="info",
        log_config=log_config,
    )
    server = uvicorn.Server(config)
    
    if shutdown_event:
        # Run with shutdown event handling
        async def shutdown_watcher():
            await shutdown_event.wait()
            server.should_exit = True
        
        await asyncio.gather(
            server.serve(),
            shutdown_watcher(),
        )
    else:
        await server.serve()


def run_server_sync():
    """Run the API server (blocking)."""
    uvicorn.run(
        "paaw.api.server:app",
        host=settings.web.host,
        port=settings.web.port,
        reload=settings.debug,
        log_level="info",
    )


if __name__ == "__main__":
    run_server_sync()
