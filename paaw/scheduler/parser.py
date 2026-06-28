"""
Job Definition Parser - Parse job.md files into structured JobDefinition.

Jobs define WHAT + WHEN + WHERE. Skills define HOW.

job.md format:
```markdown
# Job Name

## Meta
created: 2026-03-24
status: active

## Uses Skill
web_researcher

## Uses Tools
searxng, whatsapp

## Goal
What the job should accomplish.

## What To Find
- Thing 1
- Thing 2

## Delivery
- Format: bullet points
- Length: Under 1000 chars
- Only alert on: X
- Skip: Y

## Schedule
cron: 0 */4 * * *
timezone: Asia/Kolkata

## How To Notify
Post to Discord channel ID: 123456789

## Context
Additional context for the skill.
```
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class JobDefinition:
    """Parsed job definition from job.md."""
    
    # Identity
    id: str                          # Directory name (e.g., "morning_news")
    name: str                        # From # heading
    path: Path                       # Full path to job.md
    
    # Meta
    created: str = ""
    created_by: str = "system"       # "system" or "conversation"
    status: str = "active"           # active, paused, completed
    
    # Skill (HOW to do the work)
    uses_skill: str = ""             # Skill ID (e.g., "web_researcher")
    
    # MCP servers this job needs (e.g., ["searxng", "whatsapp"]). When set, the
    # executor only loads these servers' tools - keeps the prompt small so the
    # model has room to act. Empty = load all enabled MCP tools.
    uses_tools: list[str] = field(default_factory=list)
    
    # What to do (WHAT)
    goal: str = ""
    what_to_find: list[str] = field(default_factory=list)
    delivery: str = ""               # Format, length, alert rules
    context: str = ""                # Additional context
    
    # Schedule (WHEN)
    cron: str = ""                   # Cron expression
    timezone: str = "UTC"
    
    # Notification (WHERE to deliver)
    notify_instruction: str = "Notify me if you find something significant."
    
    # Legacy fields (kept for backward compatibility)
    watch_for: list[str] = field(default_factory=list)
    alert_rules: str = ""
    tools_required: list[str] = field(default_factory=list)
    related_context: dict[str, Any] = field(default_factory=dict)
    
    def is_active(self) -> bool:
        """Check if job should run."""
        return self.status == "active"
    
    def to_prompt(self) -> str:
        """Convert to prompt for LLM execution (the WHAT)."""
        # Use new format if available, fall back to legacy
        items = self.what_to_find or self.watch_for
        items_text = "\n".join(f"  - {item}" for item in items) if items else "  - (See goal)"
        
        delivery = self.delivery or self.alert_rules or "Determine what's worth sharing."
        context = self.context or self.related_context.get('notes', '')
        
        return f"""## Your Task
{self.goal}

## What To Find
{items_text}

## Delivery Requirements
{delivery}

## How To Notify
{self.notify_instruction}

## Context
{context if context else 'No additional context.'}

## Instructions
1. Use your tools to research the items above
2. Analyze findings against delivery requirements
3. If you have something significant to share, send the notification as specified
4. After notifying (or if nothing significant), respond with a brief summary
5. Start your response with [ALERT] if you notified, [NO_ALERT] if not
"""


def parse_job_md(job_path: Path) -> JobDefinition | None:
    """
    Parse a job.md file into a JobDefinition.
    
    Args:
        job_path: Path to job.md file
        
    Returns:
        JobDefinition or None if parsing fails
    """
    if not job_path.exists():
        logger.warning(f"Job file not found: {job_path}")
        return None
    
    try:
        content = job_path.read_text()
    except Exception as e:
        logger.error(f"Failed to read job file: {e}")
        return None
    
    # Extract job ID from directory name
    job_id = job_path.parent.name
    
    # Extract name from # heading
    name_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    name = name_match.group(1).strip() if name_match else job_id.replace("_", " ").title()
    
    job = JobDefinition(
        id=job_id,
        name=name,
        path=job_path,
    )
    
    # Parse sections
    sections = _extract_sections(content)
    
    # Meta section
    if "Meta" in sections:
        meta = _parse_key_values(sections["Meta"])
        job.created = meta.get("created", "")
        job.created_by = meta.get("created_by", "system")
        job.status = meta.get("status", "active")
    
    # Uses Skill (new field - HOW to do the work)
    if "Uses Skill" in sections:
        job.uses_skill = sections["Uses Skill"].strip()
    
    # Uses Tools (which MCP servers this job needs - keeps prompt small)
    if "Uses Tools" in sections:
        raw = sections["Uses Tools"].strip()
        # Support both list items ("- searxng") and inline ("searxng, whatsapp")
        items = _extract_list_items(raw)
        if not items:
            items = [t.strip() for t in re.split(r"[,\n]", raw) if t.strip()]
        job.uses_tools = items
    
    # Goal
    if "Goal" in sections:
        job.goal = sections["Goal"].strip()
    
    # What To Find (new format)
    if "What To Find" in sections:
        job.what_to_find = _extract_list_items(sections["What To Find"])
    
    # Delivery (new format - combines format, length, alert rules)
    if "Delivery" in sections:
        job.delivery = sections["Delivery"].strip()
    
    # Context (new simplified format)
    if "Context" in sections:
        job.context = sections["Context"].strip()
    
    # Watch For (legacy - list)
    if "Watch For" in sections:
        job.watch_for = _extract_list_items(sections["Watch For"])
    
    # Alert Rules (legacy)
    if "Alert Rules" in sections:
        job.alert_rules = sections["Alert Rules"].strip()
    
    # Schedule
    if "Schedule" in sections:
        schedule = _parse_key_values(sections["Schedule"])
        job.cron = schedule.get("cron", "")
        job.timezone = schedule.get("timezone", "UTC")
    
    # Alert Via (MCP server for notifications)
    if "Alert Via" in sections:
        alert_config = _parse_key_values(sections["Alert Via"])
        job.notify_instruction = alert_config.get("mcp", "web")
    elif "Alert Channel" in sections:
        # Legacy support
        job.notify_instruction = sections["Alert Channel"].strip()
    elif "How To Notify" in sections:
        # New simple format - just plain text instruction
        job.notify_instruction = sections["How To Notify"].strip()
    
    # Tools Required (legacy)
    if "Tools Required" in sections:
        job.tools_required = _extract_list_items(sections["Tools Required"])
    
    # Related Context (legacy)
    if "Related Context" in sections:
        job.related_context = _parse_key_values(sections["Related Context"])
    
    logger.info(f"Parsed job: {job.id}", status=job.status, cron=job.cron, skill=job.uses_skill)
    return job


def _extract_sections(content: str) -> dict[str, str]:
    """Extract ## sections from markdown."""
    sections = {}
    current_section = None
    current_content = []
    
    for line in content.split("\n"):
        if line.startswith("## "):
            # Save previous section
            if current_section:
                sections[current_section] = "\n".join(current_content)
            # Start new section
            current_section = line[3:].strip()
            current_content = []
        elif current_section:
            current_content.append(line)
    
    # Save last section
    if current_section:
        sections[current_section] = "\n".join(current_content)
    
    return sections


def _parse_key_values(text: str) -> dict[str, str]:
    """Parse key: value pairs from text."""
    result = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Handle "- key: value" format
        if line.startswith("- "):
            line = line[2:]
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def _extract_list_items(text: str) -> list[str]:
    """Extract - items from text."""
    items = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items


def load_all_jobs(jobs_dir: Path) -> list[JobDefinition]:
    """Load all jobs from the jobs directory."""
    jobs = []
    
    if not jobs_dir.exists():
        logger.warning(f"Jobs directory not found: {jobs_dir}")
        return jobs
    
    for job_folder in jobs_dir.iterdir():
        if not job_folder.is_dir():
            continue
        
        job_md = job_folder / "job.md"
        if not job_md.exists():
            continue
        
        job = parse_job_md(job_md)
        if job:
            jobs.append(job)
    
    logger.info(f"Loaded {len(jobs)} jobs")
    return jobs
