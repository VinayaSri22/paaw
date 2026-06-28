"""
PAAW Agent - The core agent with mental model integration.

SIMPLIFIED ARCHITECTURE (V3):
- Agent has DIRECT access to MCP tools (no sub-agents for simple queries)
- For immediate requests: LLM calls tools directly in a loop until done
- For background/scheduled work: Create a job (runs async)

Flow:
1. Check if user needs onboarding
2. Build context from mental model
3. LLM generates response, may call tools directly
4. If tools called, execute and feed results back to LLM
5. Parse mental model updates from final response
6. Only create job if user wants SCHEDULED/BACKGROUND work
"""

import re
import json
import structlog
from dataclasses import dataclass, field
from pathlib import Path

from paaw.brain.llm import LLM
from paaw.config import settings
from paaw.models import AgentResponse, ChatMessage, MessageRole, UnifiedMessage
from paaw.tools.mcp_client import MCPClient
from paaw.mental_model.conversation import ConversationManager
from paaw.scheduler.executor import _normalize_tool_parameters

logger = structlog.get_logger()

# Max number of recent conversation messages sent to the LLM per turn.
# The full history is still kept in memory and persisted to the graph;
# this only bounds the prompt size so small/local models don't get
# overwhelmed (which caused empty completions).
MAX_LLM_HISTORY_MESSAGES = 10


@dataclass
class ParsedResponse:
    """Parsed LLM response with content and metadata."""
    content: str  # The actual response text (without tags)
    nodes: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)  # New entities to create
    updates: list[dict] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    onboarding: dict | None = None
    job: dict | None = None  # Job request from LLM (skill, mode, schedule, description)


class Agent:
    """
    The PAAW Agent with mental model integration.
    
    V3 SIMPLIFIED:
    - Direct MCP tool access (no sub-agents for simple queries)
    - LLM tool calling loop for immediate requests
    - Jobs only for scheduled/background work
    """
    
    def __init__(self, graph_db=None, context_builder=None):
        self.llm = LLM()
        self.db = graph_db
        self.context_builder = context_builder
        self.conversation_history: list[ChatMessage] = []  # In-memory for current session
        self._onboarding_mode = False
        self._user_id = "user_default"
        self._channel = "web"
        
        # Conversation persistence (initialized in initialize())
        self.conversation_manager: ConversationManager | None = None
        
        # MCP client for direct tool access
        mcp_config = Path(__file__).parent.parent / "mcp" / "servers.json"
        self.mcp_client = MCPClient(mcp_config)
        self._tools_schema: list[dict] | None = None
        
        logger.info("Agent initialized", model=self.llm.model)
    
    async def initialize(self):
        """Initialize the agent with mental model components."""
        if self.db is None:
            # Import here to avoid circular imports
            from paaw.mental_model import GraphDB, ContextBuilder
            db_url = str(settings.database.url)
            self.db = await GraphDB.create(db_url)
            self.context_builder = ContextBuilder(self.db)
            logger.info("Mental model initialized")
        
        # Initialize conversation manager
        self.conversation_manager = ConversationManager(self.db, self.llm)
        
        # Load today's conversation from graph
        self.conversation_history = await self.conversation_manager.load_conversation(
            self._user_id
        )
        logger.info(
            "Conversation loaded from graph",
            user_id=self._user_id,
            messages_loaded=len(self.conversation_history),
        )
        
        # Load available tools schema for LLM
        await self._load_tools_schema()
    
    async def _load_tools_schema(self):
        """Load MCP tools and built-in tools, convert to OpenAI function calling schema."""
        if self._tools_schema is not None:
            return
        
        self._tools_schema = []
        
        # Add built-in conversation history tools
        from paaw.tools.conversation_tools import get_conversation_tools_schema
        self._tools_schema.extend(get_conversation_tools_schema())
        logger.info("Added conversation history tools")

        # Native in-process tools (web search + WhatsApp) - call HTTP services
        # directly instead of spawning an MCP container per tool.
        from paaw.tools.native_tools import get_native_tools_schema
        self._tools_schema.extend(get_native_tools_schema())
        
        # Load MCP tools
        config_path = Path(__file__).parent.parent / "mcp" / "servers.json"
        
        if not config_path.exists():
            return
        
        with open(config_path) as f:
            config = json.load(f)
        
        for server_name, server_config in config.get("mcpServers", {}).items():
            if not server_config.get("enabled", False):
                continue
            
            # Start server to get actual tool schemas
            try:
                await self.mcp_client.start_server(server_name)
                tools = await self.mcp_client.list_tools(server_name)
                
                for tool in tools:
                    # Convert MCP tool to OpenAI function schema
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
        
        logger.info(f"Loaded {len(self._tools_schema)} total tools for direct access")
    
    async def _execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool (MCP or built-in) and return the result."""
        import json
        
        logger.info(f"Executing tool: {tool_name}", arguments=arguments)
        
        # Handle built-in conversation history tools
        if tool_name == "get_conversation_history":
            from paaw.tools.conversation_tools import ConversationTools
            conv_tools = ConversationTools(self.db, self._user_id)
            result = await conv_tools.get_conversation_by_date(arguments.get("date", "today"))
            return json.dumps(result, indent=2, default=str)
        
        elif tool_name == "list_recent_conversations":
            from paaw.tools.conversation_tools import ConversationTools
            conv_tools = ConversationTools(self.db, self._user_id)
            result = await conv_tools.list_recent_conversations(arguments.get("days", 7))
            return json.dumps(result, indent=2, default=str)
        
        elif tool_name == "search_conversation_history":
            from paaw.tools.conversation_tools import ConversationTools
            conv_tools = ConversationTools(self.db, self._user_id)
            result = await conv_tools.search_conversations(arguments.get("query", ""))
            return json.dumps(result, indent=2, default=str)

        # Native in-process tools (no MCP container needed)
        from paaw.tools.native_tools import is_native_tool, execute_native_tool
        if is_native_tool(tool_name):
            return await execute_native_tool(tool_name, arguments)
        
        # Handle MCP tools (server__tool format)
        if "__" in tool_name:
            server_name, actual_tool = tool_name.split("__", 1)
        else:
            # Fallback - try to find which server has this tool
            server_name = "duckduckgo"  # Default
            actual_tool = tool_name
        
        try:
            result = await self.mcp_client.call_tool(server_name, actual_tool, arguments)
            
            # Extract content from MCP response
            if isinstance(result, dict):
                if "content" in result:
                    content = result["content"]
                    if isinstance(content, list):
                        # MCP returns list of content blocks
                        texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
                        return "\n".join(texts) if texts else str(result)
                    return str(content)
                elif "error" in result:
                    return f"Tool error: {result['error']}"
            return str(result)
            
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            return f"Tool execution failed: {e}"
    
    async def needs_onboarding(self) -> bool:
        """Check if user needs onboarding."""
        if self.db is None:
            return False
        return not await self.db.user_exists(self._user_id)
    
    async def process(self, message: UnifiedMessage) -> AgentResponse:
        """
        Process a message with direct tool access.
        
        V3 FLOW:
        1. Build context from mental model
        2. Call LLM with tools available
        3. If LLM wants to call tools, execute them and loop
        4. When LLM gives final response, parse for mental model updates
        5. Only create job if user explicitly wants background/scheduled work
        """
        logger.info(
            "Processing message",
            channel=message.channel,
            content_length=len(message.content),
        )
        
        # Check for onboarding
        if self.db and await self.needs_onboarding():
            return await self._handle_onboarding(message)
        
        # Add user message to history
        user_msg = ChatMessage(
            role=MessageRole.USER,
            content=message.content,
            timestamp=message.timestamp,
        )
        self.conversation_history.append(user_msg)
        
        # Persist user message to graph
        if self.conversation_manager:
            await self.conversation_manager.save_message(
                user_id=self._user_id,
                message=user_msg,
                channel=message.channel,
            )
        
        # Build context from mental model
        system_prompt = await self._build_system_prompt(message.content)
        
        try:
            # Tool calling loop - LLM can call tools directly
            max_iterations = 5
            iteration = 0
            final_content = ""
            
            # Track conversation for tool calls (separate from main history)
            tool_messages = []
            
            # Track tools used in this conversation turn
            tools_used_this_turn = []
            
            while iteration < max_iterations:
                iteration += 1
                
                # Prepare messages for this iteration.
                # Only send a recent window of history to the LLM (the full
                # history stays in self.conversation_history and the graph).
                # This keeps the prompt small enough for local/small models.
                messages_for_llm = self.conversation_history[-MAX_LLM_HISTORY_MESSAGES:]
                messages_for_llm = messages_for_llm.copy()
                messages_for_llm.extend(tool_messages)
                
                # Call LLM with tools if available. In lite mode we omit tools
                # entirely so the prompt stays small enough for tiny-context models.
                use_tools = (not settings.lite_mode) and bool(self._tools_schema)
                response = await self.llm.chat(
                    messages=messages_for_llm,
                    system_prompt=system_prompt,
                    tools=self._tools_schema if use_tools else None,
                    return_full_response=True,
                )
                
                content = response.get("content", "")
                tool_calls = response.get("tool_calls")
                
                # If no tool calls, we're done
                if not tool_calls:
                    final_content = content
                    break
                
                logger.info(f"LLM requested {len(tool_calls)} tool call(s)")
                
                # Add assistant message with tool calls to conversation
                tool_messages.append(ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=content or "",
                    tool_calls=[{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in tool_calls]
                ))
                
                # Execute each tool call
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        arguments = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                    
                    # Track tool usage
                    tools_used_this_turn.append(tool_name)
                    
                    # Execute the tool
                    result = await self._execute_tool(tool_name, arguments)
                    
                    # Add tool result to conversation
                    tool_messages.append(ChatMessage(
                        role=MessageRole.TOOL,
                        content=result,
                        tool_call_id=tool_call.id,
                    ))
                
                # If we got content along with tool calls, include it
                if content:
                    final_content = content
            
            # Parse the final response for mental model updates
            parsed = self._parse_response(final_content)
            
            # Fallback: never return a silent empty reply. Small/local models
            # sometimes return empty content (or content that is entirely tags).
            if not parsed.content.strip():
                logger.warning(
                    "Empty response from LLM - using fallback",
                    raw_length=len(final_content or ""),
                    model=self.llm.model,
                )
                parsed.content = (
                    "Sorry, I couldn't generate a reply to that. "
                    "Could you rephrase or try again?"
                )
            
            # Add clean response to history
            assistant_msg = ChatMessage(
                role=MessageRole.ASSISTANT,
                content=parsed.content,
            )
            self.conversation_history.append(assistant_msg)
            
            # Persist assistant message to graph
            if self.conversation_manager:
                await self.conversation_manager.save_message(
                    user_id=self._user_id,
                    message=assistant_msg,
                    channel=message.channel,
                    tools_used=tools_used_this_turn,
                )
            
            # Process mental model updates
            await self._process_mental_model_updates(parsed)
            
            logger.info(
                "Response generated",
                response_length=len(parsed.content),
                iterations=iteration,
                entities_created=len(parsed.entities),
                memories_created=len(parsed.memories),
                updates_made=len(parsed.updates),
                tools_used=tools_used_this_turn,
            )
            
            return AgentResponse(
                content=parsed.content,
                model_used=self.llm.model,
                tools_used=tools_used_this_turn,
            )
            
        except Exception as e:
            logger.error("Failed to process message", error=str(e), exc_info=True)
            return AgentResponse(
                content=f"I encountered an error: {e}. Please try again.",
                model_used=self.llm.model,
            )
    
    async def process_stream(self, message: UnifiedMessage):
        """Process a message and stream the response."""
        logger.info("Processing message (streaming)", channel=message.channel)
        
        # Check for onboarding
        if self.db and await self.needs_onboarding():
            response = await self._handle_onboarding(message)
            yield response.content
            return
        
        # Add user message to history
        user_msg = ChatMessage(
            role=MessageRole.USER,
            content=message.content,
            timestamp=message.timestamp,
        )
        self.conversation_history.append(user_msg)
        
        # Persist user message to graph
        if self.conversation_manager:
            await self.conversation_manager.save_message(
                user_id=self._user_id,
                message=user_msg,
                channel=message.channel,
            )
        
        # Build context
        system_prompt = await self._build_system_prompt(message.content)
        
        # Stream response - collect full text, filter display
        full_response = ""
        display_buffer = ""
        stop_display = False  # Once we hit internal tags, stop showing anything
        
        async for chunk in self.llm.chat_stream(
            messages=self.conversation_history[-MAX_LLM_HISTORY_MESSAGES:],
            system_prompt=system_prompt,
        ):
            full_response += chunk
            
            # If we've already hit tags, don't display anymore
            if stop_display:
                continue
                
            display_buffer += chunk
            
            # Check if we've hit an internal tag
            internal_tags = ['<entity>', '<memory>', '<update>', '<nodes>', '<job>', '<onboarding>']
            for tag in internal_tags:
                if tag in display_buffer:
                    # Found a tag - yield everything before it, then stop
                    before_tag = display_buffer.split(tag)[0]
                    if before_tag.strip():
                        yield before_tag
                    stop_display = True
                    break
            
            if stop_display:
                continue
                
            # Check for partial tag at end (< without closing >)
            if '<' in display_buffer:
                last_open = display_buffer.rfind('<')
                after_open = display_buffer[last_open:]
                if '>' not in after_open:
                    # Partial tag - yield everything before it, keep the rest
                    yield display_buffer[:last_open]
                    display_buffer = after_open
                    continue
            
            # Safe to yield the whole buffer
            if display_buffer:
                yield display_buffer
                display_buffer = ""
        
        # Parse and process
        parsed = self._parse_response(full_response)
        
        # Add to history
        assistant_msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=parsed.content,
        )
        self.conversation_history.append(assistant_msg)
        
        # Persist assistant message to graph
        if self.conversation_manager:
            await self.conversation_manager.save_message(
                user_id=self._user_id,
                message=assistant_msg,
                channel=message.channel,
            )
        
        # Process updates
        await self._process_mental_model_updates(parsed)
    
    async def _build_system_prompt(self, user_message: str) -> str:
        """Build system prompt with mental model context."""
        if self.context_builder is None:
            # Fallback to basic prompt
            from paaw.brain.prompts import get_system_prompt
            return get_system_prompt(f"User ID: {self._user_id}")
        
        ctx = await self.context_builder.build_context(
            user_message=user_message,
            user_id=self._user_id,
            lite=settings.lite_mode,
        )
        
        logger.debug(
            "Context built",
            keywords=ctx.keywords,
            matched_nodes=[n.label for n in ctx.matched_nodes],
        )
        
        return ctx.system_prompt
    
    async def _handle_onboarding(self, message: UnifiedMessage) -> AgentResponse:
        """Handle onboarding flow for new users."""
        from paaw.onboarding import OnboardingFlow
        
        flow = OnboardingFlow(self.db, self.llm)
        
        # Check if this is the first interaction (empty message or greeting-like)
        # vs a response to our greeting (user introducing themselves)
        user_msg = message.content.strip().lower()
        is_greeting_only = len(user_msg) < 10 or user_msg in ['hi', 'hello', 'hey', 'start', 'begin']
        
        if is_greeting_only:
            # First message - send greeting
            greeting = flow.get_greeting()
            
            self.conversation_history.append(
                ChatMessage(role=MessageRole.ASSISTANT, content=greeting)
            )
            
            return AgentResponse(content=greeting, model_used=self.llm.model)
        
        # User is introducing themselves - process it
        try:
            result = await flow.process_introduction(
                user_response=message.content,
                user_id=self._user_id,
            )
            
            response = flow.get_confirmation_prompt(result)
            
            self.conversation_history.append(
                ChatMessage(role=MessageRole.USER, content=message.content)
            )
            self.conversation_history.append(
                ChatMessage(role=MessageRole.ASSISTANT, content=response)
            )
            
            logger.info(
                "Onboarding complete",
                user_name=result.user_name,
            )
            
            return AgentResponse(content=response, model_used=self.llm.model)
            
        except Exception as e:
            logger.error("Onboarding failed", error=str(e))
            return AgentResponse(
                content=f"Sorry, I had trouble processing that. Could you try again? ({e})",
                model_used=self.llm.model,
            )
    
    def _parse_response(self, response: str) -> ParsedResponse:
        """
        Parse LLM response for content and tags.
        
        Extracts (in order of processing):
        - <entity>...</entity>  (new nodes to create)
        - <nodes>...</nodes>    (relevant node IDs)
        - <update>...</update>  (updates to existing nodes)
        - <memory>...</memory>  (memories to attach)
        - <job>...</job>        (job request from LLM)
        - <onboarding>...</onboarding>
        """
        result = ParsedResponse(content=response)
        
        # Extract all <entity> blocks (for creating new nodes)
        entity_pattern = r'<entity>(.*?)</entity>'
        for match in re.finditer(entity_pattern, response, re.DOTALL):
            entity_data = self._parse_yaml_block(match.group(1))
            if entity_data:
                result.entities.append(entity_data)
            response = response.replace(match.group(0), '')
        
        # Extract <nodes>
        nodes_match = re.search(r'<nodes>(.*?)</nodes>', response, re.DOTALL)
        if nodes_match:
            nodes_str = nodes_match.group(1).strip()
            result.nodes = [n.strip() for n in nodes_str.split(',') if n.strip()]
            response = response.replace(nodes_match.group(0), '')
        
        # Extract all <update> blocks
        update_pattern = r'<update>(.*?)</update>'
        for match in re.finditer(update_pattern, response, re.DOTALL):
            update_data = self._parse_yaml_block(match.group(1))
            if update_data:
                result.updates.append(update_data)
            response = response.replace(match.group(0), '')
        
        # Extract all <memory> blocks
        memory_pattern = r'<memory>(.*?)</memory>'
        for match in re.finditer(memory_pattern, response, re.DOTALL):
            memory_data = self._parse_yaml_block(match.group(1))
            if memory_data:
                result.memories.append(memory_data)
            response = response.replace(match.group(0), '')
        
        # Extract <job> if present (LLM decided we need a job)
        # Format: <job skill="x" mode="y" schedule="z">description</job>
        job_match = re.search(r'<job\s+([^>]*)>(.*?)</job>', response, re.DOTALL)
        if job_match:
            attrs_str = job_match.group(1)
            description = job_match.group(2).strip()
            
            # Parse attributes
            job_data = {"description": description}
            for attr_match in re.finditer(r'(\w+)=["\']([^"\']*)["\']', attrs_str):
                job_data[attr_match.group(1)] = attr_match.group(2)
            
            result.job = job_data
            response = response.replace(job_match.group(0), '')
            logger.info(f"Parsed job request from LLM: {job_data}")
        
        # Extract <onboarding> if present
        onboard_match = re.search(r'<onboarding>(.*?)</onboarding>', response, re.DOTALL)
        if onboard_match:
            result.onboarding = self._parse_yaml_block(onboard_match.group(1))
            response = response.replace(onboard_match.group(0), '')
        
        # Clean up response
        result.content = response.strip()
        
        return result
    
    def _parse_yaml_block(self, text: str) -> dict | None:
        """Parse a YAML-like block into a dict."""
        try:
            result = {}
            current_key = None
            
            for line in text.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                
                if ':' in line:
                    key, _, value = line.partition(':')
                    key = key.strip()
                    value = value.strip()
                    
                    if value:
                        result[key] = value
                    else:
                        current_key = key
                        result[key] = []
                elif line.startswith('-') and current_key:
                    result[current_key].append(line[1:].strip())
            
            return result if result else None
        except Exception:
            return None
    
    async def _process_mental_model_updates(self, parsed: ParsedResponse):
        """
        Process mental model updates from parsed response.
        
        IMPORTANT: Order matters!
        1. Entities first (create new nodes)
        2. Updates (modify existing nodes)
        3. Memories last (link to nodes created above)
        """
        if self.db is None:
            return
        
        from paaw.mental_model.models import NodeType, EdgeType
        
        # 1. FIRST: Process entities (create new nodes)
        created_entities = {}  # Track created entities for memory linking
        for entity in parsed.entities:
            entity_type = entity.get('type', 'Domain')
            label = entity.get('label', 'Unknown')
            parent = entity.get('parent', self._user_id)
            context = entity.get('context', '')
            attributes = entity.get('attributes', {})
            
            # Parse attributes if it's a string (from YAML-like parsing)
            if isinstance(attributes, str):
                # Try to parse "key: value" pairs
                attrs = {}
                for line in attributes.split('\n'):
                    if ':' in line:
                        k, _, v = line.partition(':')
                        attrs[k.strip()] = v.strip()
                attributes = attrs
            
            # Generate node ID from type and label
            node_id = self._generate_node_id(entity_type, label)
            
            # Check if node already exists
            if await self.db.node_exists(node_id):
                logger.info(f"Entity {node_id} already exists, skipping creation")
                created_entities[label.lower()] = node_id
                continue
            
            # Map type string to NodeType enum
            try:
                node_type = NodeType(entity_type)
            except ValueError:
                node_type = NodeType.DOMAIN  # Default fallback
            
            # Create the node
            await self.db.create_node(
                id=node_id,
                node_type=node_type,
                label=label,
                context=context,
                attributes=attributes,
            )
            
            # Link to parent
            if parent and await self.db.node_exists(parent):
                await self.db.create_edge(node_id, parent, EdgeType.CHILD_OF)
                await self.db.create_edge(parent, node_id, EdgeType.HAS_CHILD)
            
            created_entities[label.lower()] = node_id
            logger.info(f"Created entity: {node_id} ({entity_type}) -> parent: {parent}")
        
        # Record node access
        if parsed.nodes:
            await self.db.record_access(parsed.nodes)
            logger.debug(f"Recorded access to nodes: {parsed.nodes}")
        
        # 2. Process updates
        for update in parsed.updates:
            node_id = update.get('node_id')
            field = update.get('field')
            new_value = update.get('new_value')
            
            if node_id and field and new_value:
                if field == 'context':
                    await self.db.update_node(node_id, context=new_value)
                elif field == 'key_facts':
                    facts = [f.strip() for f in new_value.split(',')]
                    await self.db.update_node(node_id, key_facts=facts)
                elif field.startswith('attributes.'):
                    attr_name = field.split('.', 1)[1]
                    await self.db.update_node(node_id, attributes={attr_name: new_value})
                elif field == 'status':
                    await self.db.update_node(node_id, attributes={'status': new_value})
                
                logger.info(f"Updated node {node_id}: {field} = {new_value}")
        
        # 3. LAST: Process memories (link to nodes that now exist)
        for memory in parsed.memories:
            content = memory.get('content')
            mem_type = memory.get('type', 'fact')
            belongs_to = memory.get('belongs_to', '')
            
            if content:
                node_ids = [n.strip() for n in belongs_to.split(',') if n.strip()]
                await self.db.add_memory(
                    content=content,
                    memory_type=mem_type,
                    belongs_to=node_ids,
                    user_id=self._user_id,
                )
                logger.info(f"Added memory: {content[:50]}...")
    
    def _generate_node_id(self, node_type: str, label: str) -> str:
        """Generate a consistent node ID from type and label."""
        # Normalize: lowercase, replace spaces with underscores, remove special chars
        clean_label = label.lower()
        clean_label = re.sub(r'[^a-z0-9\s_]', '', clean_label)
        clean_label = re.sub(r'\s+', '_', clean_label).strip('_')
        # Truncate to reasonable length
        clean_label = clean_label[:30]
        return f"{node_type.lower()}_{clean_label}"
    
    def clear_history(self) -> None:
        """Clear conversation history."""
        self.conversation_history = []
        self._onboarding_mode = False
        logger.info("Conversation history cleared")
    
    @property
    def history_length(self) -> int:
        """Get the number of messages in history."""
        return len(self.conversation_history)
