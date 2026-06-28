"""
Native (in-process) tools for PAAW.

Instead of spawning a separate MCP adapter container per tool (via `docker run`),
these tools call the existing long-running HTTP services directly:

  - searxng__searxng_web_search / searxng__web_url_read
        -> the SearXNG service (SEARXNG_URL, default http://searxng:8080)
  - whatsapp__send_whatsapp_to_me / _message / _to_group / list_whatsapp_groups
        -> the WhatsApp bot bridge (WHATSAPP_BRIDGE_URL, default http://whatsapp:3000)

This keeps the deployment lean - no ephemeral `vigilant_lamarr`-style containers.

Tool names intentionally keep the `searxng__` / `whatsapp__` prefixes so existing
job `## Uses Tools` filters and skill prompts keep working unchanged.
"""

import os
import re

import httpx
import structlog

logger = structlog.get_logger()

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
WHATSAPP_BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp:3000").rstrip("/")

_HTTP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_SEARXNG_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "searxng__searxng_web_search",
            "description": (
                "Search the web via SearXNG and return titles, URLs and snippets. "
                "Use the parameter name exactly `query`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query string."},
                    "num_results": {
                        "type": "integer",
                        "description": "Max results to return (1-20).",
                        "default": 7,
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Optional recency filter.",
                        "enum": ["day", "week", "month", "year"],
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "searxng__web_url_read",
            "description": (
                "Fetch a URL and return its readable text content. Use only when "
                "search snippets aren't enough."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to read."},
                    "max_length": {
                        "type": "integer",
                        "description": "Max characters of text to return.",
                        "default": 8000,
                    },
                },
                "required": ["url"],
            },
        },
    },
]

_WHATSAPP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "whatsapp__send_whatsapp_to_me",
            "description": (
                "Send a WhatsApp message to the owner (PAAW user). Delivers to the "
                "configured chat for the active mode (Note-to-Self, the PAAW group, "
                "or your number). Use this for job notifications."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message text to send to the owner."},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whatsapp__send_whatsapp_message",
            "description": "Send a WhatsApp message to a phone number (country code, no +, e.g. 919876543210).",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Phone number with country code, no +."},
                    "message": {"type": "string", "description": "Message text to send."},
                },
                "required": ["phone", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whatsapp__send_whatsapp_to_group",
            "description": "Send a WhatsApp message to a group by name (partial, case-insensitive match).",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_name": {"type": "string", "description": "Group name (partial match supported)."},
                    "message": {"type": "string", "description": "Message text to send."},
                },
                "required": ["group_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whatsapp__list_whatsapp_groups",
            "description": "List available WhatsApp groups. Use to find the correct group name before sending.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def get_native_tools_schema(
    include_searxng: bool = True,
    include_whatsapp: bool = True,
) -> list[dict]:
    """Return OpenAI function schemas for the enabled native tools."""
    schema: list[dict] = []
    if include_searxng:
        schema.extend(_SEARXNG_TOOLS)
    if include_whatsapp:
        schema.extend(_WHATSAPP_TOOLS)
    return schema


_NATIVE_TOOL_NAMES = {
    t["function"]["name"]
    for t in (_SEARXNG_TOOLS + _WHATSAPP_TOOLS)
}


def is_native_tool(name: str) -> bool:
    """True if this tool is handled in-process (not via an MCP server)."""
    return name in _NATIVE_TOOL_NAMES


# ---------------------------------------------------------------------------
# SearXNG
# ---------------------------------------------------------------------------

async def _searxng_web_search(query: str, num_results: int = 7, time_range: str | None = None) -> str:
    params: dict[str, str] = {"q": query, "format": "json"}
    if time_range:
        params["time_range"] = time_range

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(f"{SEARXNG_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])[: max(1, min(int(num_results or 7), 20))]
    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("content") or "").strip()
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\n\s*\n\s*\n+")


def _html_to_text(html: str) -> str:
    """Very small HTML -> text cleaner (no extra deps)."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    # Decode a few common entities
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(ent, ch)
    # Collapse whitespace
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _WS_RE.sub("\n\n", text)
    return text.strip()


async def _web_url_read(url: str, max_length: int = 8000) -> str:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PAAW/1.0)"})
        resp.raise_for_status()
        html = resp.text

    text = _html_to_text(html)
    limit = max(500, int(max_length or 8000))
    if len(text) > limit:
        text = text[:limit] + f"\n\n[... truncated, {len(text) - limit} chars omitted]"
    return text or "(No readable text content found.)"


# ---------------------------------------------------------------------------
# WhatsApp bridge
# ---------------------------------------------------------------------------

async def _bridge_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(f"{WHATSAPP_BRIDGE_URL}{path}", json=body)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code >= 400:
            raise RuntimeError(data.get("error") or f"bridge error {resp.status_code}")
        return data


async def _bridge_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(f"{WHATSAPP_BRIDGE_URL}{path}")
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code >= 400:
            raise RuntimeError(data.get("error") or f"bridge error {resp.status_code}")
        return data


async def _send_whatsapp_to_me(message: str) -> str:
    result = await _bridge_post("/send", {"message": message})
    return f"Message sent to owner ({result.get('to', 'default target')})."


async def _send_whatsapp_message(phone: str, message: str) -> str:
    await _bridge_post("/send", {"phone": phone, "message": message})
    return f"Message sent to {phone}."


async def _send_whatsapp_to_group(group_name: str, message: str) -> str:
    result = await _bridge_post("/send", {"group": group_name, "message": message})
    return f"Message sent to group \"{result.get('groupName', group_name)}\"."


async def _list_whatsapp_groups() -> str:
    data = await _bridge_get("/groups")
    groups = data.get("groups", [])
    if not groups:
        return "No groups found."
    return "Available groups:\n" + "\n".join(
        f"- {g.get('name')} ({g.get('participantCount', '?')} members)" for g in groups
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute_native_tool(name: str, arguments: dict) -> str:
    """Execute a native tool and return a string result."""
    arguments = arguments or {}
    try:
        if name == "searxng__searxng_web_search":
            return await _searxng_web_search(
                query=arguments.get("query", ""),
                num_results=arguments.get("num_results", 7),
                time_range=arguments.get("time_range"),
            )
        if name == "searxng__web_url_read":
            return await _web_url_read(
                url=arguments.get("url", ""),
                max_length=arguments.get("max_length", 8000),
            )
        if name == "whatsapp__send_whatsapp_to_me":
            return await _send_whatsapp_to_me(message=arguments.get("message", ""))
        if name == "whatsapp__send_whatsapp_message":
            return await _send_whatsapp_message(
                phone=arguments.get("phone", ""),
                message=arguments.get("message", ""),
            )
        if name == "whatsapp__send_whatsapp_to_group":
            return await _send_whatsapp_to_group(
                group_name=arguments.get("group_name", ""),
                message=arguments.get("message", ""),
            )
        if name == "whatsapp__list_whatsapp_groups":
            return await _list_whatsapp_groups()
        return f"Unknown native tool: {name}"
    except Exception as e:
        logger.warning(f"Native tool {name} failed: {e}")
        return f"Tool error: {e}"
