# Track Berkshire Hathaway - New CEO Watch

## Meta
created: 2026-03-24
created_by: server_room
status: active

## Uses Skill
web_researcher

## Uses Tools
searxng, whatsapp

## Goal
Quick daily check on Berkshire Hathaway news. Flag anything that deviates from Buffett's value investing philosophy.

## What To Find
- Any major Berkshire Hathaway news from the last 24 hours
- New investments or acquisitions (if any)
- Any red flags against Buffett's philosophy (speculation, crypto, excessive debt)

## Delivery
- Format: 3-5 bullet points max
- Length: Under 500 characters
- Only alert on: Major portfolio changes, philosophy violations, CEO statements
- If nothing significant: Send "No major Berkshire updates today"

## Schedule
cron: 0 8 * * *
timezone: Asia/Kolkata

## How To Notify
IMPORTANT: After completing your research, you MUST send the results to me on
WhatsApp using the send_whatsapp_to_me tool. It delivers to my configured chat
automatically (Note-to-Self, the PAAW group, or my number - depending on mode).
Always send a message - either the full briefing or "No major Berkshire updates
today" if nothing significant.

## Context
Buffett's philosophy: long-term value investing, avoid speculation/crypto, no excessive debt, stay in circle of competence.