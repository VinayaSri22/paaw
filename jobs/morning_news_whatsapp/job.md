# Morning News Briefing (WhatsApp)

## Meta
created: 2026-06-28
created_by: system
status: paused

## Uses Skill
web_researcher

## Uses Tools
searxng, whatsapp

## Goal
Find top tech and AI news of the day. Keep the user informed without overwhelming.

## What To Find
- Global tech news (Google, Apple, Microsoft, OpenAI)
- AI/ML developments and breakthroughs
- India-specific tech news if relevant

## Delivery
- Format: 5-7 key headlines with one-line summaries as bullet points
- Length: Under 1500 characters
- Only alert on: Breaking tech news, major AI announcements
- Skip: Routine product updates, minor market movements

## Schedule
cron: 0 8 * * *
timezone: Asia/Kolkata

## How To Notify
IMPORTANT: After completing your research, you MUST send the results to me on
WhatsApp using the send_whatsapp_to_me tool. It delivers to my configured chat
automatically (Note-to-Self, the PAAW group, or my number - depending on mode).
Always send a message - either the full briefing or "No major updates today" if
nothing significant.

## Context
User is a tech professional. Prefers bullet points over paragraphs. This is a
quick morning briefing, not a deep dive.

## Note
This is the WhatsApp twin of the `morning_news` job (which uses Discord).
It is "paused" by default - set status to "active" and enable the `whatsapp`
MCP server in mcp/servers.json to use it. See mcp/whatsapp-baileys/README.md.
