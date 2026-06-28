# PAAW WhatsApp Integration (Baileys)

Chat with PAAW over WhatsApp **and** let scheduled jobs notify you on WhatsApp —
the same way the Discord integration works.

This package runs as a single long-lived service (`whatsapp`) that owns **one**
WhatsApp connection and does two things:

1. **Inbound chat** — listens for your messages and forwards them to PAAW's
   `/api/chat`, then replies.
2. **Outbound bridge** — a tiny HTTP server (`:3000`) so PAAW **jobs/MCP** can
   send WhatsApp messages through that same connection.

> ⚠️ **Why one connection?** Opening a second WhatsApp session against the same
> account corrupts the Signal sessions (`Bad MAC` / `No session record` errors).
> So the job-facing MCP server never connects to WhatsApp directly — it calls the
> bridge instead.

---

## Architecture

```
                         ┌──────────────────────────────────────────┐
                         │        whatsapp service (Node)           │
   Your WhatsApp ──────▶ │  bot.js                                  │
        ▲                │   • inbound: messages → PAAW /api/chat   │
        │  reply         │   • outbound bridge: HTTP :3000          │
        └─────────────── │   • ONE Baileys connection (auth/)       │
                         └───────────────▲──────────────────────────┘
                                         │ HTTP POST /send
                         ┌───────────────┴──────────────────────────┐
                         │  PAAW job executor                       │
                         │   spawns whatsapp MCP (index.js)         │
                         │   → tools: send_whatsapp_to_me, etc.     │
                         └──────────────────────────────────────────┘
```

---

## Modes

Set `WHATSAPP_MODE` in `.env`. All three are supported; pick whichever you like.

| Mode | Bot runs on | You talk to PAAW in | 2nd number? | Notes |
|------|-------------|---------------------|-------------|-------|
| `self_chat` (default) | **Your** number | "Message Yourself" (Note to Self) | No | Simplest. Shares space with your personal notes. |
| `group` | **Your** number | A dedicated group (default name `PAAW`) | No | Clean separate space. Recommended for single-number setups. |
| `dedicated` | A **separate** number | Message PAAW's number like a contact | Yes | Cleanest UX, no echo issues, reliable decryption. |

In `self_chat` and `group`, your messages and PAAW's replies are both
`fromMe: true`, so an **echo guard** (tracking sent message IDs) prevents PAAW
from replying to itself.

---

## Configuration (`.env`)

```bash
# Pick a mode
WHATSAPP_MODE=self_chat        # self_chat | group | dedicated

# Owner identity (your personal WhatsApp)
#   self_chat: optional (auto-detected); set OWNER_LID if detection misses
#   dedicated: OWNER_NUMBER REQUIRED (the number you message FROM)
OWNER_NUMBER=918555934326      # country code, no +
OWNER_LID=226589623238805      # your @lid (from the bot logs)

# group mode only
PAAW_GROUP_NAME=PAAW           # group name to match (default: PAAW)
# PAAW_GROUP_JID=120363xxx@g.us  # pin exact id (most reliable)
```

| Variable | Mode | Required | Description |
|----------|------|----------|-------------|
| `WHATSAPP_MODE` | all | no | `self_chat` (default), `group`, or `dedicated` |
| `OWNER_NUMBER` | dedicated | **yes** | Your personal number (country code, no `+`) |
| `OWNER_NUMBER` | self_chat | no | Override if auto-detect misses your number |
| `OWNER_LID` | dedicated / self_chat | often **yes** | Your `@lid` number. Modern WhatsApp delivers the sender as a privacy `@lid`, not the phone number — if your messages don't match `OWNER_NUMBER`, set this. See [Finding your LID](#finding-your-whatsapp-lid). |
| `PAAW_GROUP_NAME` | group | no | Group name to match (default `PAAW`) |
| `PAAW_GROUP_JID` | group | no | Exact group JID; most reliable matcher |
| `PAAW_URL` | all | no | PAAW API (default `http://paaw:8080` in Docker) |
| `HTTP_PORT` | all | no | Outbound bridge port (default `3000`) |

---

## Finding your WhatsApp LID

Modern WhatsApp often identifies a sender by a **LID** (a privacy ID like
`477602880xxxx@lid`) instead of their phone number (`919xxxxxxx17@s.whatsapp.net`).
When that happens, matching on `OWNER_NUMBER` fails and PAAW ignores you — so you
need to set `OWNER_LID`.

**How to find it (takes 30 seconds):**

1. Start the bot and stream logs:
   ```bash
   docker compose up -d --force-recreate whatsapp
   docker compose logs -f whatsapp
   ```
2. From the number that should be allowed (in `dedicated` mode, your personal
   number; in `self_chat`, yourself), **send any message** to PAAW.
3. In the logs, find the `🔎 upsert` line for **your** message — the one with
   `fromMe=false` (dedicated) and look at `jid=`:
   ```
   🔎 upsert type=notify jid=47760xxxxx51349@lid fromMe=false hasText=true ...
                             ^^^^^^^^^^^^^^^  ← this is your LID
   ```
   - If `jid` ends in `@s.whatsapp.net` → `OWNER_NUMBER` already matches; no LID needed.
   - If `jid` ends in `@lid` → copy the number before `@lid`.

**Where to add it** — in `.env`:
```bash
OWNER_LID=47760xxxxx51349     # the number before @lid (digits only, no @lid)
```

Then recreate so it takes effect:
```bash
docker compose up -d --force-recreate whatsapp
```

> The LID is **per-account**: each WhatsApp number has its own LID. If you switch
> which number talks to PAAW (e.g. re-link to a different number), grab the new
> LID from the logs again. Setting the wrong account's LID (a common mistake when
> switching modes) silently blocks messages.

---

## Setup

### Mode A — Self-chat (no second number)

```bash
# .env
WHATSAPP_MODE=self_chat
# OWNER_LID=226589623238805   # add after first run if needed

docker compose up -d --build whatsapp
docker compose logs -f whatsapp     # scan the QR
```

Talk to PAAW in WhatsApp → **Message Yourself**.

### Mode B — Dedicated group (no second number, clean space)

1. In WhatsApp, create a group named **PAAW** (add anyone to create it, then
   remove them so you're alone).
2. Configure and start:
   ```bash
   # .env
   WHATSAPP_MODE=group
   PAAW_GROUP_NAME=PAAW

   docker compose up -d --build whatsapp
   docker compose logs -f whatsapp
   ```
3. Confirm the logs show `Group JID: …@g.us` (not `NOT FOUND`). If not found,
   send a message in the group, copy the `@g.us` id from the `🔎 upsert` log,
   and set `PAAW_GROUP_JID`.

Talk to PAAW in the **PAAW** group.

### Mode C — Dedicated number (real bot)

```bash
# .env
WHATSAPP_MODE=dedicated
OWNER_NUMBER=919xxxxxxx87     # YOUR personal number (the one you message FROM)

docker compose stop whatsapp
rm -rf mcp/whatsapp-baileys/auth/*   # fresh link
docker compose up -d --build whatsapp
docker compose logs -f whatsapp      # scan QR with PAAW's SECOND number
```

Message PAAW's (second) number from your personal WhatsApp like any contact.

> **Likely needed:** your first message probably won't get a reply because
> WhatsApp delivers your number as a `@lid`. Check the logs for your message's
> `jid=...@lid`, set `OWNER_LID` (see [Finding your LID](#finding-your-whatsapp-lid)),
> and recreate. After that PAAW responds normally.

---

## Job / Skill / MCP notifications (parity with Discord)

PAAW jobs notify via WhatsApp through the `whatsapp` MCP server, which forwards
to the bridge. Tools available to jobs:

| Tool | Purpose |
|------|---------|
| `send_whatsapp_to_me` | Send to the owner (default chat for the active mode). **Use this for job notifications.** |
| `send_whatsapp_message` | Send to a specific phone number |
| `send_whatsapp_to_group` | Send to a group by name |
| `list_whatsapp_groups` | List groups |

### Enable it

1. Make sure the `whatsapp` service is running (it owns the session + bridge).
2. In `mcp/servers.json`, set the `whatsapp` server `"enabled": true`.
3. Restart PAAW so the executor picks up the new tool.

> **Important (Docker):** the job executor launches MCP servers with `docker run`,
> so the `paaw` container needs the Docker CLI **and** the host docker socket
> mounted (`/var/run/docker.sock`). This is already configured in
> `docker-compose.yaml` (the `paaw` service installs `docker-ce-cli` and mounts
> the socket). Without it you'll see `Command not found: docker` and
> `Loaded 0 tools for job execution`, and jobs can't notify anywhere (WhatsApp
> *or* Discord).

> **Model note:** sending requires the job's LLM to actually emit a tool call.
> Very small local models (e.g. `gemma-4-e4b`) often narrate instead of calling
> tools, or exhaust their token budget on reasoning (`finish_reason: length`)
> before calling `send_whatsapp_to_me`. Use a capable model for jobs (Claude,
> GPT, or a larger local model) for reliable delivery.

### Use it in a job (`jobs/<name>/job.md`)

```markdown
## How To Notify
After completing your research, send the summary to me on WhatsApp using the
send_whatsapp_to_me tool. Always send something - the briefing, or
"No major updates today" if nothing significant.
```

That's the WhatsApp equivalent of the Discord `## How To Notify` block.

---

## How sending works (the bridge)

`bot.js` exposes:

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| GET | `/health` | — | `{ ok, connected, mode }` |
| GET | `/groups` | — | `{ groups: [...] }` |
| POST | `/send` | `{ message }` | Send to default target (notify owner) |
| POST | `/send` | `{ phone, message }` | Send to a phone number |
| POST | `/send` | `{ group, message }` | Send to a group by name |
| POST | `/send` | `{ to, message }` | Send to a raw JID |

Quick test:

```bash
curl -s localhost:3000/health
curl -s -X POST localhost:3000/send \
  -H 'Content-Type: application/json' \
  -d '{"message":"Hello from the bridge"}'
```

---

## Files

| File | Role |
|------|------|
| `whatsapp.js` | Baileys client: connect, QR, modes, gating, send helpers |
| `bot.js` | Inbound chat loop + outbound HTTP bridge |
| `index.js` | MCP server (HTTP client to the bridge) for jobs |
| `Dockerfile` | Node 20 image (`paaw/whatsapp:latest`) |
| `auth/` | Session credentials (gitignored — never commit) |

---

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| `Bad MAC` / `No session record` floods | Stale/corrupted session from re-linking, or two sessions on one account. Do a clean re-link: `docker compose stop whatsapp && rm -rf mcp/whatsapp-baileys/auth/* && docker compose up -d whatsapp`. Never run a second connection on the same number. |
| PAAW ignores your messages (dedicated/self-chat) | Your number arrives as a privacy `@lid`, not the phone number. Find it in the logs and set `OWNER_LID` — see [Finding your LID](#finding-your-whatsapp-lid). A stale LID from a previous number is a common cause. |
| Group `NOT FOUND` | Create the group / fix the name, or set `PAAW_GROUP_JID` to the exact id. |
| PAAW replies to itself | Echo guard should prevent this; ensure you're on the latest `whatsapp.js`. |
| Job can't send WhatsApp | Ensure `whatsapp` service is up, MCP `enabled:true`, `:3000` reachable (`curl localhost:3000/health`), and the `paaw` container has the docker socket mounted (`Command not found: docker` / `Loaded 0 tools` means it doesn't). |
| Job runs but `tools_used: []` / nothing sent | The LLM didn't emit a tool call. Small local models often narrate or hit `finish_reason: length` before calling tools — use a stronger job model. |
| QR not scannable in terminal | Open the saved image: `mcp/whatsapp-baileys/auth/qr.png`. |

---

## Notes & risks

- Baileys is an **unofficial** WhatsApp client. Keep volume reasonable to reduce
  ban risk; a dedicated/secondary number is safest.
- The `auth/` folder holds your session — treat it like a password, never commit it.
- All channels (web, CLI, Discord, WhatsApp) map to the single `user_default`
  profile, so PAAW's mental model stays consistent across them.
