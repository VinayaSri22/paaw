"""
Job Executor - Execute jobs in isolated contexts.

Key principle: Jobs are NOT conversations.
- No conversation history
- Fresh context each run
- Results stored as Trail nodes
- Significant findings as Memory nodes
- Alerts sent separately (not injected into chat)

Jobs define WHAT/WHEN/WHERE, Skills define HOW.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from paaw.brain.llm import LLM
from paaw.scheduler.parser import JobDefinition
from paaw.scheduler.skills import SkillDefinition, load_skill
from paaw.tools.mcp_client import MCPClient

logger = structlog.get_logger()


def _normalize_tool_parameters(input_schema: dict | None) -> dict:
    """Ensure an MCP tool's JSON schema is valid for OpenAI function calling.

    Some MCP servers return `parameters` as `{"type": "object"}` with no
    `properties` key. Strict providers (e.g. LM Studio) reject the whole
    request with a 400 unless `properties` is present, so we backfill it.
    """
    schema = dict(input_schema) if isinstance(input_schema, dict) else {}
    if not schema:
        return {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    if schema.get("type") == "object" and "properties" not in schema:
        schema["properties"] = {}
    return schema


@dataclass
class ExecutionResult:
    """Result of job execution."""
    job_id: str
    timestamp: datetime
    status: str                      # completed, failed, skipped
    duration_seconds: float
    tools_used: list[str] = field(default_factory=list)
    summary: str = ""
    should_alert: bool = False
    alert_message: str = ""
    error: str | None = None
    token_usage: int = 0             # Total tokens used across all LLM calls


class JobExecutor:
    """
    Execute jobs in isolated contexts.
    
    Each job runs with:
    - Its own LLM conversation (no shared history)
    - Access to specified tools only
    - User's mental model context (read-only)
    - Per-job locking to prevent duplicate runs
    """
    
    def __init__(self, graph_db=None):
        from paaw.config import settings
        self.db = graph_db
        # Jobs are agentic (need solid tool-calling). Allow a dedicated job model
        # (LLM_JOB_MODEL) so jobs can use a capable model even if chat uses a
        # small local one. Falls back to default_model when unset.
        self.llm = LLM(model=settings.llm.job_model)
        
        # MCP client for tool access
        mcp_config = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        self.mcp_client = MCPClient(mcp_config)
        
        # Per-job locks to prevent concurrent runs of same job
        self._job_locks: dict[str, asyncio.Lock] = {}
        
        # Tools schema cache
        self._tools_schema: list[dict] | None = None
    
    async def initialize(self):
        """Initialize executor (load tools, connect to DB)."""
        if self.db is None:
            from paaw.mental_model import GraphDB
            from paaw.config import settings
            db_url = str(settings.database.url)
            self.db = await GraphDB.create(db_url)
        
        await self._load_tools_schema()
        logger.info("JobExecutor initialized")
    
    async def _load_tools_schema(self):
        """Load available MCP tools."""
        if self._tools_schema is not None:
            return
        
        self._tools_schema = []
        config_path = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
        
        if not config_path.exists():
            return
        
        with open(config_path) as f:
            config = json.load(f)
        
        for server_name, server_config in config.get("mcpServers", {}).items():
            if not server_config.get("enabled", False):
                continue
            
            try:
                await self.mcp_client.start_server(server_name)
                tools = await self.mcp_client.list_tools(server_name)
                
                for tool in tools:
                    self._tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": f"{server_name}__{tool['name']}",
                            "description": tool.get("description", ""),
                            "parameters": _normalize_tool_parameters(tool.get("inputSchema")),
                        }
                    })
            except Exception as e:
                logger.warning(f"Failed to load tools from {server_name}: {e}")
        
        logger.info(f"Loaded {len(self._tools_schema)} tools for job execution")
    
    def _get_job_lock(self, job_id: str) -> asyncio.Lock:
        """Get or create lock for a job."""
        if job_id not in self._job_locks:
            self._job_locks[job_id] = asyncio.Lock()
        return self._job_locks[job_id]
    
    async def execute(self, job: JobDefinition, user_id: str = "user_default") -> ExecutionResult:
        """
        Execute a job with locking.
        
        Args:
            job: The job definition to execute
            user_id: The user this job belongs to
            
        Returns:
            ExecutionResult with status and findings
        """
        lock = self._get_job_lock(job.id)
        
        # Try to acquire lock without waiting
        if lock.locked():
            logger.info(f"Job {job.id} already running, skipping")
            return ExecutionResult(
                job_id=job.id,
                timestamp=datetime.utcnow(),
                status="skipped",
                duration_seconds=0,
                summary="Job already running",
            )
        
        async with lock:
            return await self._execute_job(job, user_id)
    
    async def _execute_job(self, job: JobDefinition, user_id: str) -> ExecutionResult:
        """Actually execute the job (called with lock held)."""
        start_time = datetime.utcnow()
        tools_used = []
        total_tokens = 0  # Accumulate tokens across all LLM calls
        
        logger.info(f"Executing job: {job.id}", user_id=user_id, skill=job.uses_skill)
        
        try:
            # Load skill if specified (HOW to do the work)
            skill: SkillDefinition | None = None
            if job.uses_skill:
                skill = load_skill(job.uses_skill)
                if skill:
                    logger.info(f"Loaded skill: {skill.id} for job {job.id}")
                else:
                    logger.warning(f"Skill not found: {job.uses_skill}")
            
            # Build job context (NOT conversation history)
            user_context = await self._get_user_context(user_id)
            
            # Build the system prompt - skill provides HOW, job provides WHAT
            system_prompt = self._build_system_prompt(job, user_context, skill)
            user_prompt = job.to_prompt()
            
            # Tool selection: if the job declares the MCP servers it needs
            # (## Uses Tools), only pass those tools. This keeps the prompt small
            # (a big tool schema can fill the whole context window of small models
            # and leave no room to actually call a tool). Empty = all tools.
            job_tools = self._tools_schema
            if job.uses_tools and self._tools_schema:
                allowed = set(job.uses_tools)
                job_tools = [
                    t for t in self._tools_schema
                    if t["function"]["name"].split("__", 1)[0] in allowed
                ]
                logger.info(
                    f"Job {job.id} restricted to tools from: {sorted(allowed)} "
                    f"({len(job_tools)}/{len(self._tools_schema)} tools)"
                )
            
            # Tool loop - LLM keeps going until it's done (no tool calls)
            # No arbitrary iteration limit - trust the LLM to finish
            # Safety: 20 iterations max to prevent infinite loops (edge case)
            messages = [{"role": "user", "content": user_prompt}]
            final_content = ""
            max_safety = 20  # Safety net, not design constraint
            
            for _ in range(max_safety):
                response = await self.llm.chat(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=job_tools if job_tools else None,
                    return_full_response=True,
                )
                
                content = response.get("content", "")
                tool_calls = response.get("tool_calls")
                
                # Accumulate token usage across all LLM calls in this job
                usage = response.get("usage")
                if usage:
                    total_tokens += usage.get("total_tokens", 0) or 0
                
                # Done when LLM returns content without requesting tools
                if not tool_calls:
                    final_content = content
                    break
                
                # LLM wants to use tools - execute them
                messages.append({
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                        for tc in tool_calls
                    ]
                })
                
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tools_used.append(tool_name)
                    
                    try:
                        arguments = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                    
                    result = await self._execute_tool(tool_name, arguments)
                    
                    # Summarize large tool results to prevent context explosion
                    if len(result) > 1500:
                        result = await self._summarize_tool_result(tool_name, result)
                    
                    messages.append({
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tool_call.id,
                    })
                
                # Keep last content in case we hit safety limit
                if content:
                    final_content = content
            
            # Parse result for alert
            should_alert = "[ALERT]" in final_content
            
            # Extract alert message or summary
            if should_alert:
                alert_message = final_content.replace("[ALERT]", "").strip()
                summary = f"Alert: {alert_message[:100]}..."
            else:
                alert_message = ""
                summary = final_content.replace("[NO_ALERT]", "").strip()[:200]
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            result = ExecutionResult(
                job_id=job.id,
                timestamp=start_time,
                status="completed",
                duration_seconds=duration,
                tools_used=list(set(tools_used)),
                summary=summary,
                should_alert=should_alert,
                alert_message=alert_message,
                token_usage=total_tokens,
            )
            
            # Store trail in graph
            await self._store_trail(result, user_id)
            
            # Store memory if significant
            if should_alert:
                await self._store_finding(job, result, user_id)
            
            logger.info(
                f"Job completed: {job.id}",
                duration=duration,
                should_alert=should_alert,
                tools_used=tools_used,
                token_usage=total_tokens,
            )
            
            return result
            
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            logger.error(f"Job failed: {job.id}", error=str(e))
            
            result = ExecutionResult(
                job_id=job.id,
                timestamp=start_time,
                status="failed",
                duration_seconds=duration,
                tools_used=tools_used,
                summary=f"Job failed: {str(e)}",
                error=str(e),
                token_usage=total_tokens,
            )
            
            await self._store_trail(result, user_id)
            return result
    
    def _build_system_prompt(self, job: JobDefinition, user_context: dict, skill: SkillDefinition | None = None) -> str:
        """
        Build system prompt for job execution.
        
        If a skill is specified, it provides the HOW (persona, approach).
        Job provides the WHAT (goal, delivery requirements).
        """
        # Start with skill persona if available
        if skill:
            skill_section = skill.to_system_prompt()
        else:
            skill_section = """## Your Role
You are PAAW, executing a scheduled background job. Use your tools efficiently to complete the task."""
        
        return f"""{skill_section}

## Job Execution Context
You are running as a background job, not in conversation.

IMPORTANT:
- Be efficient with tool calls (max 10-12 total) - prefer search snippets over fetching full pages
- Follow the job's "## How To Notify" section EXACTLY - it names the tool to use
  (e.g. send_whatsapp_to_me, or a Discord/email tool). Call ONLY that tool.
- Use ONLY tools that are actually available to you. Do not invent or call tools
  that were not provided (e.g. don't call Discord tools for a WhatsApp job).
- Sending the notification is your PRIMARY deliverable - always complete it.
- After sending, respond with [ALERT] if significant news, [NO_ALERT] if routine update
- Keep your final response concise (this runs automatically)

## User Context
Name: {user_context.get('name', 'User')}
Key facts: {', '.join(user_context.get('key_facts', [])[:5]) or 'None available'}
"""
    
    async def _get_user_context(self, user_id: str) -> dict:
        """Get minimal user context for job execution."""
        if not self.db:
            return {"name": "User", "key_facts": []}
        
        try:
            user_node = await self.db.get_user_node(user_id)
            if user_node:
                return {
                    "name": user_node.label,
                    "key_facts": user_node.key_facts or [],
                }
        except Exception as e:
            logger.warning(f"Failed to get user context: {e}")
        
        return {"name": "User", "key_facts": []}
    
    def _filter_tools(self, required_tools: list[str]) -> list[dict]:
        """Filter tools schema to only what job needs."""
        if not self._tools_schema or not required_tools:
            return self._tools_schema or []
        
        # Match tools by name (without server prefix)
        filtered = []
        for tool in self._tools_schema:
            tool_name = tool["function"]["name"]
            # Check if any required tool matches (with or without server prefix)
            actual_name = tool_name.split("__")[-1] if "__" in tool_name else tool_name
            if actual_name in required_tools or tool_name in required_tools:
                filtered.append(tool)
        
        return filtered if filtered else self._tools_schema
    
    async def _summarize_tool_result(self, tool_name: str, result: str) -> str:
        """
        Summarize a large tool result using a local model.
        
        Uses local LLM (configured via LLM_SUMMARIZER_MODEL) to avoid
        rate limits on the main API. Falls back to truncation.
        """
        from paaw.config import settings
        from litellm import acompletion
        
        # Build summarization prompt based on tool type
        if "search" in tool_name.lower():
            prompt = f"""Summarize these search results concisely.
Extract: title, key point, URL for the most relevant 5-7 results.
Max 400 words.

Search Results:
{result[:5000]}"""
        else:
            prompt = f"""Summarize this tool output concisely.
Keep essential information only. Max 400 words.

Tool Output:
{result[:5000]}"""
        
        try:
            summarizer_model = getattr(settings.llm, 'summarizer_model', None)
            summarizer_base_url = getattr(settings.llm, 'summarizer_base_url', None)
            
            if summarizer_model and summarizer_base_url:
                # Use local model for summarization
                response = await acompletion(
                    model=summarizer_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=600,
                    api_base=summarizer_base_url,
                )
                summary = response.choices[0].message.content or ""
                logger.info(f"Summarized tool result: {len(result)} -> {len(summary)} chars")
                return f"[Summarized] {summary}"
            else:
                # No summarizer configured - truncate
                return result[:1500] + "\n[... truncated]"
                
        except Exception as e:
            logger.warning(f"Failed to summarize: {e}, truncating instead")
            return result[:1500] + "\n[... truncated]"
    
    async def _execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool and return result."""
        logger.info(f"Job executing tool: {tool_name}")
        
        if "__" in tool_name:
            server_name, actual_tool = tool_name.split("__", 1)
        else:
            server_name = "duckduckgo"
            actual_tool = tool_name
        
        try:
            result = await self.mcp_client.call_tool(server_name, actual_tool, arguments)
            
            if isinstance(result, dict):
                if "content" in result:
                    content = result["content"]
                    if isinstance(content, list):
                        texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
                        text = "\n".join(texts) if texts else str(result)
                    else:
                        text = str(content)
                elif "error" in result:
                    return f"Tool error: {result['error']}"
                else:
                    text = str(result)
            else:
                text = str(result)
            
            # Only truncate extremely long results (like full web pages)
            # Search results are usually fine, but fetch_content can be huge
            max_chars = 8000  # ~2000 tokens - enough for most content
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n[Content truncated for brevity - {len(text) - max_chars} chars omitted]"
            
            return text
            
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            return f"Tool error: {e}"
    
    async def _store_trail(self, result: ExecutionResult, user_id: str):
        """Store execution trail in graph."""
        if not self.db:
            return
        
        try:
            from paaw.mental_model.models import NodeType, EdgeType
            
            trail_id = f"trail_{result.job_id}_{result.timestamp.strftime('%Y%m%d_%H%M%S')}"
            
            await self.db.create_node(
                id=trail_id,
                node_type=NodeType.TRAIL,
                label=f"Trail: {result.job_id}",
                context=result.summary,
                attributes={
                    "job_id": result.job_id,
                    "status": result.status,
                    "duration_seconds": result.duration_seconds,
                    "tools_used": result.tools_used,
                    "alert_sent": result.should_alert,
                    "error": result.error,
                    "token_usage": result.token_usage,
                },
            )
            
            # Link trail to job
            job_node_id = f"job_{result.job_id}"
            await self.db.create_edge(job_node_id, trail_id, EdgeType.HAS_TRAIL)
            
            logger.debug(f"Stored trail: {trail_id}")
            
        except Exception as e:
            logger.warning(f"Failed to store trail: {e}")
    
    async def _store_finding(self, job: JobDefinition, result: ExecutionResult, user_id: str):
        """Store significant finding as memory."""
        if not self.db or not result.alert_message:
            return
        
        try:
            # Get related nodes from job context
            related_nodes = []
            if job.related_context.get("domain"):
                related_nodes.append(f"domain_{job.related_context['domain']}")
            related_nodes.append(f"job_{job.id}")
            
            memory_id = await self.db.add_memory(
                content=result.alert_message,
                memory_type="finding",
                belongs_to=related_nodes,
                source_channel=f"job_{job.id}",
            )
            
            logger.debug(f"Stored finding as memory: {memory_id}")
            
        except Exception as e:
            logger.warning(f"Failed to store finding: {e}")
    
    async def cleanup(self):
        """Cleanup resources."""
        if self.mcp_client:
            await self.mcp_client.stop_all()
