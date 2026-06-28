"""
Context Builder - Assembles system prompt from mental model.

Strategy:
1. Root nodes always in prompt (~500 tokens)
2. Matched nodes from keyword search added with details
3. Recent memories for matched nodes included
4. Available skills and tools for LLM to decide when to use
5. Instructions for LLM to output <nodes>, <update>, and <job> blocks
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from paaw.mental_model.graph import GraphDB
from paaw.mental_model.models import BaseNode, NodeType
from paaw.mental_model.search import NodeSearch, extract_keywords

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Context assembled for a conversation turn."""
    system_prompt: str
    user_node: BaseNode | None
    root_nodes: list[BaseNode]
    matched_nodes: list[BaseNode]
    matched_memories: list[BaseNode]
    keywords: list[str]
    skills: list[dict] = field(default_factory=list)  # Available skills
    tools: list[dict] = field(default_factory=list)   # Available MCP tools


class ContextBuilder:
    """
    Builds context for LLM from mental model.
    
    Every message gets:
    - User profile (always)
    - Root-level nodes summary (always, ~500 tokens)
    - Detailed context for keyword-matched nodes
    - Recent memories for matched nodes
    - Available skills and MCP tools (so LLM can decide to create jobs)
    """
    
    def __init__(self, graph_db: GraphDB, skills_dir: Path | None = None, mcp_config_path: Path | None = None):
        self.db = graph_db
        self.search = NodeSearch(graph_db)
        
        # Default paths
        base_dir = Path(__file__).parent.parent.parent
        self.skills_dir = skills_dir or base_dir / "skills"
        self.mcp_config_path = mcp_config_path or base_dir / "mcp" / "servers.json"
        
        # Cache skills and tools
        self._skills_cache: list[dict] | None = None
        self._tools_cache: list[dict] | None = None
    
    def _load_skills(self) -> list[dict]:
        """Load available skills from skills directory."""
        if self._skills_cache is not None:
            return self._skills_cache
        
        skills = []
        if self.skills_dir.exists():
            for skill_dir in self.skills_dir.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "skill.md"
                    if skill_file.exists():
                        skill_id = skill_dir.name
                        content = skill_file.read_text()
                        
                        # Parse basic info from markdown
                        name = skill_id.replace("_", " ").title()
                        persona = ""
                        keywords = []
                        
                        for line in content.split("\n"):
                            if line.startswith("# "):
                                name = line[2:].strip()
                            elif "## Persona" in content:
                                # Get persona section
                                start = content.find("## Persona")
                                end = content.find("##", start + 10)
                                if end == -1:
                                    end = len(content)
                                persona = content[start+10:end].strip()[:200]
                            elif line.startswith("## Keywords"):
                                # Get next line with keywords
                                idx = content.find("## Keywords")
                                if idx != -1:
                                    kw_section = content[idx:].split("\n")[1:3]
                                    for kw_line in kw_section:
                                        if kw_line.strip() and not kw_line.startswith("#"):
                                            keywords = [k.strip() for k in kw_line.split(",")][:10]
                                            break
                        
                        skills.append({
                            "id": skill_id,
                            "name": name,
                            "persona": persona[:150] if persona else f"Skill for {name}",
                            "keywords": keywords,
                        })
        
        self._skills_cache = skills
        return skills
    
    def _load_mcp_tools(self) -> list[dict]:
        """Load available MCP tools from config."""
        if self._tools_cache is not None:
            return self._tools_cache
        
        tools = []
        if self.mcp_config_path.exists():
            try:
                with open(self.mcp_config_path) as f:
                    config = json.load(f)
                
                for server_name, server_config in config.get("mcpServers", {}).items():
                    if server_config.get("enabled", False):
                        for tool in server_config.get("tools", []):
                            tools.append({
                                "name": tool,
                                "server": server_name,
                                "description": server_config.get("description", ""),
                            })
            except Exception as e:
                logger.warning(f"Failed to load MCP config: {e}")
        
        self._tools_cache = tools
        return tools
    
    async def build_context(
        self,
        user_message: str,
        user_id: str = "user_default",
        include_instructions: bool = True,
        lite: bool = False,
    ) -> ConversationContext:
        """
        Build complete context for a conversation turn.
        
        Args:
            user_message: The user's message
            user_id: The user's node ID
            include_instructions: Whether to include LLM output instructions
            
        Returns:
            ConversationContext with system prompt and metadata
        """
        # Get user node
        user_node = await self.db.get_user_node(user_id)
        
        # Get root-level nodes
        root_nodes = await self.db.get_root_nodes(user_id)
        
        # Lite mode: compact prompt for small-context local models.
        # Skip semantic search, memories, capabilities and tag instructions -
        # just give the model the user's identity and high-level mental model.
        if lite:
            keywords = extract_keywords(user_message)
            system_prompt = self._build_system_prompt(
                user_node=user_node,
                root_nodes=root_nodes,
                matched_nodes=[],
                matched_memories=[],
                skills=[],
                tools=[],
                include_instructions=False,
            )
            return ConversationContext(
                system_prompt=system_prompt,
                user_node=user_node,
                root_nodes=root_nodes,
                matched_nodes=[],
                matched_memories=[],
                keywords=keywords,
                skills=[],
                tools=[],
            )
        
        # Extract keywords and search for relevant nodes
        keywords = extract_keywords(user_message)
        search_results = await self.search.search(user_message, limit=5)
        matched_nodes = [r.node for r in search_results]
        
        # Get recent memories for matched nodes
        matched_memories = []
        for node in matched_nodes[:3]:  # Limit to top 3
            memories = await self.db.get_recent_memories(node.id, limit=3)
            matched_memories.extend(memories)
        
        # Load available skills and tools
        skills = self._load_skills()
        tools = self._load_mcp_tools()
        
        # Build the system prompt
        system_prompt = self._build_system_prompt(
            user_node=user_node,
            root_nodes=root_nodes,
            matched_nodes=matched_nodes,
            matched_memories=matched_memories,
            skills=skills,
            tools=tools,
            include_instructions=include_instructions,
        )
        
        return ConversationContext(
            system_prompt=system_prompt,
            user_node=user_node,
            root_nodes=root_nodes,
            matched_nodes=matched_nodes,
            matched_memories=matched_memories,
            keywords=keywords,
            skills=skills,
            tools=tools,
        )
    
    def _build_system_prompt(
        self,
        user_node: BaseNode | None,
        root_nodes: list[BaseNode],
        matched_nodes: list[BaseNode],
        matched_memories: list[BaseNode],
        skills: list[dict],
        tools: list[dict],
        include_instructions: bool = True,
    ) -> str:
        """Build the complete system prompt."""
        parts = []
        
        # Base personality
        parts.append(self._base_prompt())
        
        # User profile
        if user_node:
            parts.append(self._user_profile(user_node))
        else:
            parts.append("USER: New user - start with onboarding.\n")
        
        # Root nodes summary (always included)
        if root_nodes:
            parts.append(self._root_nodes_summary(root_nodes))
        
        # Matched nodes with details
        if matched_nodes:
            parts.append(self._matched_nodes_section(matched_nodes, matched_memories))
        
        # Available skills and tools (V2: LLM decides when to use)
        if skills or tools:
            parts.append(self._capabilities_section(skills, tools))
        
        # Output instructions
        if include_instructions:
            parts.append(self._output_instructions())
        
        return "\n".join(parts)
    
    def _capabilities_section(self, skills: list[dict], tools: list[dict]) -> str:
        """Format available skills and tools section."""
        lines = ["\n--- YOUR CAPABILITIES ---"]
        lines.append("")
        
        if tools:
            lines.append("TOOLS YOU CAN USE DIRECTLY:")
            lines.append("You have direct access to these tools via function calling.")
            lines.append("Use them immediately when the user needs current information.")
            lines.append("")
            for tool in tools:
                lines.append(f"  • {tool['name']}: {tool.get('description', '')}")
            lines.append("")
        
        if skills:
            lines.append("BACKGROUND SKILLS (configured by user in Server Room):")
            for skill in skills:
                lines.append(f"  • {skill['id']}: {skill.get('persona', '')[:80]}")
            lines.append("")
        
        lines.append("HOW TO USE TOOLS:")
        lines.append("- User asks a question needing current info → use search tool directly")
        lines.append("- User wants to know something now → call the tool and respond")
        lines.append("- For scheduled/recurring tasks, tell user to configure a Job in the Server Room")
        
        return "\n".join(lines)
    
    def _base_prompt(self) -> str:
        """Base personality and capabilities."""
        from datetime import datetime
        current_date = datetime.now().strftime("%B %d, %Y")
        current_time = datetime.now().strftime("%H:%M")
        
        return f"""You are PAAW (Personal AI Assistant that Works) - an AI that actually remembers.

CURRENT DATE & TIME: {current_date} at {current_time}
(Use this for any time-sensitive questions or research requests)

Unlike other AI assistants, you have a mental model of this user built from every conversation.
You remember their life, goals, relationships, preferences, and history.

Your personality:
- Warm and genuine, like a friend who truly knows them
- Direct and helpful - no fluff
- Proactive when you notice patterns or connections
- Honest about what you remember or don't know

PAAW ARCHITECTURE (be aware of this):
- You are built on an MCP (Model Context Protocol) architecture
- Your capabilities come from MCP servers that provide tools (search, calendar, email, etc.)
- If user asks for something you don't have access to, tell them they can ADD an MCP server for it
- Example: "I don't have calendar access yet, but you can add a Google Calendar MCP server to give me that ability!"
- Never say "the platform would need to" - YOU are PAAW, and MCP servers extend YOUR capabilities directly

You can:
- Remember and connect information across conversations
- Track tasks and follow up on them
- Notice patterns in their life and gently point them out
- Help them achieve their goals by keeping context
- Be extended with new capabilities via MCP servers (just add to mcp/servers.json)"""
    
    def _user_profile(self, user: BaseNode) -> str:
        """Format user profile section."""
        lines = ["\n--- USER PROFILE ---"]
        lines.append(f"Name: {user.label}")
        
        # Add attributes
        if user.attributes.get("location"):
            lines.append(f"Location: {user.attributes['location']}")
        if user.attributes.get("timezone"):
            lines.append(f"Timezone: {user.attributes['timezone']}")
        if user.attributes.get("languages"):
            lines.append(f"Languages: {', '.join(user.attributes['languages'])}")
        if user.attributes.get("response_style"):
            lines.append(f"Prefers: {user.attributes['response_style']}")
        
        # Key facts
        if user.key_facts:
            lines.append("\nKey facts:")
            for fact in user.key_facts[:5]:
                lines.append(f"  • {fact}")
        
        # Context (LLM understanding)
        if user.context:
            lines.append(f"\nYour understanding: {user.context}")
        
        return "\n".join(lines)
    
    def _root_nodes_summary(self, nodes: list[BaseNode]) -> str:
        """Format root nodes as compact summary."""
        lines = ["\n--- MENTAL MODEL (root level) ---"]
        
        # Group by type
        by_type: dict[str, list[BaseNode]] = {}
        for node in nodes:
            type_name = node.type.value
            if type_name not in by_type:
                by_type[type_name] = []
            by_type[type_name].append(node)
        
        # Type emojis
        emojis = {
            "Domain": "🌐",
            "Person": "👤",
            "Project": "📁",
            "Goal": "🎯",
            "Task": "📋",
        }
        
        for type_name, type_nodes in by_type.items():
            emoji = emojis.get(type_name, "•")
            items = []
            for node in type_nodes:
                # Include node ID for reference
                status = node.attributes.get("status", "")
                status_str = f" [{status}]" if status else ""
                items.append(f"{node.label} (id: {node.id}){status_str}")
            
            lines.append(f"{emoji} {type_name}: {', '.join(items)}")
        
        return "\n".join(lines)
    
    def _matched_nodes_section(
        self,
        nodes: list[BaseNode],
        memories: list[BaseNode],
    ) -> str:
        """Format detailed context for matched nodes."""
        lines = ["\n--- RELEVANT CONTEXT (from your mental model) ---"]
        
        for node in nodes:
            lines.append(f"\n[{node.type.value}] {node.label} (id: {node.id})")
            if node.context:
                lines.append(f"  Understanding: {node.context}")
            if node.key_facts:
                for fact in node.key_facts[:3]:
                    lines.append(f"  • {fact}")
        
        # Recent memories
        if memories:
            lines.append("\nRecent memories:")
            for mem in memories[:5]:
                lines.append(f"  - {mem.context[:80]}")
        
        return "\n".join(lines)
    
    def _output_instructions(self) -> str:
        """Instructions for LLM output format."""
        return """
--- OUTPUT INSTRUCTIONS ---
After your response, include these structured tags as needed.

1. NEW ENTITIES - Create for important people, projects, goals:
   <entity>
   type: Person|Domain|Goal|Project|Task
   label: Human readable name (keep simple)
   parent: node_id of parent (usually user_default)
   context: Your understanding of this entity
   attributes:
     key: value (relationship, status, etc)
   </entity>
   
   **CRITICAL**: When user mentions family members (mom, dad, siblings, spouse, etc.), 
   ALWAYS create a Person entity for them!
   
   Node IDs are auto-generated as: {type}_{label_lowercase_underscored}
   Example: label "Dad" → id becomes "person_dad"

2. RELEVANT NODES (always):
   <nodes>user_default,other_node_ids</nodes>

3. UPDATES (when existing info changes):
   <update>
   node_id: the_node_id
   field: context|key_facts|attributes.field_name
   new_value: the updated value
   </update>

4. NEW MEMORIES (facts worth remembering):
   <memory>
   content: what to remember (specific and atomic)
   type: fact|observation|preference|episode
   belongs_to: node_id (must exist or be created above)
   </memory>

NOTE: For scheduled/recurring tasks (daily briefings, reminders, monitoring), 
tell the user to configure a Job in the Server Room UI. You cannot create jobs directly.

EXAMPLE - User asks for research:

"I'd be happy to research that for you! Let me look into it."
[Then use your search tools directly to find the answer]

<nodes>user_default</nodes>"""
    
    async def build_onboarding_prompt(self) -> str:
        """Build the prompt for onboarding a new user."""
        return """You are PAAW (Personal AI Assistant that Works).

This is a NEW USER. Start with onboarding.

Say something warm like:
"Hey! I'm PAAW, your personal AI assistant. Unlike other AIs, I actually remember you - every conversation helps me understand you better.

Tell me about yourself! What's your name, what do you do, what's keeping you busy these days? The more you share, the better I can help."

After they respond, you'll extract:
- Their name and basic info
- Key people in their life
- Current projects/work
- Goals or things they're working towards
- Any preferences mentioned

Output the extracted info as:
<onboarding>
user:
  name: Their name
  location: If mentioned
  timezone: If mentioned or inferred
  
people:
  - label: Name
    relationship: How they relate
    context: What we learned
    
domains:
  - label: Domain name (Work, Health, Family, etc)
    context: What we learned
    
projects:
  - label: Project name
    status: active/planned
    context: What we learned
    
goals:
  - label: Goal description
    status: active
    context: Details
    
observations:
  - Pattern or preference noticed
</onboarding>

Keep the conversation natural. You can ask follow-up questions to learn more."""
    
    def format_for_logging(self, ctx: ConversationContext) -> str:
        """Format context for debug logging."""
        lines = [
            f"Keywords: {ctx.keywords}",
            f"User: {ctx.user_node.label if ctx.user_node else 'None'}",
            f"Root nodes: {len(ctx.root_nodes)}",
            f"Matched nodes: {[n.label for n in ctx.matched_nodes]}",
            f"Matched memories: {len(ctx.matched_memories)}",
        ]
        return " | ".join(lines)
