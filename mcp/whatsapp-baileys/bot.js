/**
 * WhatsApp Bot for PAAW.
 *
 * Two jobs in one process (sharing ONE Baileys connection):
 *  1. Inbound chat: listens for owner messages -> PAAW /api/chat -> replies.
 *  2. Outbound bridge: tiny HTTP server so PAAW jobs/MCP can send WhatsApp
 *     messages WITHOUT opening a second WhatsApp session (which would corrupt
 *     the signal sessions).
 *
 * Usage:
 *   node bot.js
 *
 * Environment:
 *   PAAW_URL  - PAAW API URL (default: http://localhost:8080)
 *   HTTP_PORT - Outbound bridge port (default: 3000)
 */

import http from 'http';
import { getWhatsAppClient } from './whatsapp.js';

const PAAW_URL = process.env.PAAW_URL || 'http://localhost:8080';
const HTTP_PORT = parseInt(process.env.HTTP_PORT || '3000', 10);
const TIMEOUT_MS = 120000; // 2 minutes for LLM responses

console.log('🐾 PAAW WhatsApp Bot');
console.log(`   PAAW API: ${PAAW_URL}`);
console.log(`   Bridge port: ${HTTP_PORT}`);
console.log('');

/**
 * Call PAAW's chat API.
 * @param {string} message - User's message
 * @param {string} userId - WhatsApp JID (used as user identifier)
 * @param {string} username - Push name
 * @returns {string|null} - PAAW's response or null on error
 */
async function callPAAW(message, userId, username) {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), TIMEOUT_MS);

    const response = await fetch(`${PAAW_URL}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        // Owner-only channel: always the single PAAW user, so the mental
        // model stays consistent across web/CLI/Discord/WhatsApp.
        user_id: 'user_default',
        channel: 'whatsapp',
        metadata: {
          username,
          platform: 'whatsapp',
        },
      }),
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      const errText = await response.text().catch(() => '');
      console.error(`PAAW API error: ${response.status} ${errText.slice(0, 300)}`);
      return null;
    }

    const data = await response.json();
    const reply = data.response || data.message || null;
    if (!reply) {
      // Surface what PAAW actually returned so we can diagnose empty replies
      console.error('PAAW returned no usable reply. Raw:', JSON.stringify(data).slice(0, 400));
    }
    return reply;
  } catch (err) {
    if (err.name === 'AbortError') {
      console.error('PAAW API timeout (LLM took >120s)');
      return "Sorry, I'm taking too long to respond. Try again?";
    }
    console.error('Error calling PAAW:', err.message);
    return null;
  }
}

/**
 * Handle incoming WhatsApp messages.
 */
async function handleMessage(jid, text, pushName, isGroup) {
  // This only fires for the owner's self-chat (gated in whatsapp.js).
  console.log(`💬 Processing message from ${pushName}...`);

  const response = await callPAAW(text, jid, pushName);

  if (response) {
    const client = getWhatsAppClient();
    
    // WhatsApp doesn't have strict char limits like Discord,
    // but very long messages can be split for readability
    const MAX_LENGTH = 4000;
    
    if (response.length <= MAX_LENGTH) {
      await client.sendMessage(jid, response);
    } else {
      // Split into chunks
      const chunks = [];
      for (let i = 0; i < response.length; i += MAX_LENGTH) {
        chunks.push(response.slice(i, i + MAX_LENGTH));
      }
      
      for (const chunk of chunks) {
        await client.sendMessage(jid, chunk);
        // Small delay between chunks
        await new Promise(r => setTimeout(r, 500));
      }
    }
    
    console.log(`✅ Responded to owner`);
  } else {
    console.log(`⚠️ No response from PAAW`);
  }
}

/**
 * Read and JSON-parse an HTTP request body.
 */
function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, payload) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(payload));
}

/**
 * Outbound HTTP bridge - lets PAAW jobs/MCP send WhatsApp messages through
 * the single live connection. Endpoints:
 *   GET  /health             -> { ok, connected, mode }
 *   GET  /groups             -> { groups: [...] }
 *   POST /send  { message, to? , phone?, group? }
 *        - no target  -> default target for the active mode (notify owner)
 *        - phone      -> a specific number (country code, no +)
 *        - group      -> a group by name (partial match)
 *        - to         -> a raw JID
 */
function startBridge(client) {
  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === 'GET' && req.url === '/health') {
        return sendJson(res, 200, {
          ok: true,
          connected: client.connected,
          mode: client.mode,
        });
      }

      if (req.method === 'GET' && req.url === '/groups') {
        const groups = await client.listGroups();
        return sendJson(res, 200, { groups });
      }

      if (req.method === 'POST' && req.url === '/send') {
        if (!client.connected) {
          return sendJson(res, 503, { error: 'WhatsApp not connected' });
        }

        const body = await readJsonBody(req);
        const message = (body.message || '').toString();
        if (!message) {
          return sendJson(res, 400, { error: 'message is required' });
        }

        // Resolve target
        if (body.group) {
          const result = await client.sendToGroup(body.group, message);
          if (result.error) return sendJson(res, 404, result);
          return sendJson(res, 200, { ok: true, ...result });
        }

        let targetJid = body.to || null;
        if (!targetJid && body.phone) {
          targetJid = `${body.phone.toString().replace(/[^0-9]/g, '')}@s.whatsapp.net`;
        }
        if (!targetJid) targetJid = client.defaultTarget;

        if (!targetJid) {
          return sendJson(res, 400, {
            error: 'No target resolved. Provide phone/group/to, or configure the active mode.',
          });
        }

        await client.sendMessage(targetJid, message);
        return sendJson(res, 200, { ok: true, to: targetJid });
      }

      sendJson(res, 404, { error: 'Not found' });
    } catch (err) {
      console.error('Bridge error:', err);
      sendJson(res, 500, { error: err.message });
    }
  });

  server.listen(HTTP_PORT, () => {
    console.log(`🌉 Outbound bridge listening on :${HTTP_PORT}`);
  });

  return server;
}

/**
 * Main entry point.
 */
async function main() {
  const client = getWhatsAppClient();

  // Connect with message handler
  await client.connect(handleMessage);

  // Start the outbound HTTP bridge for jobs/MCP
  startBridge(client);

  console.log('');
  console.log('📱 WhatsApp bot is running!');
  console.log('   Send a message to your WhatsApp number to chat with PAAW.');
  console.log('');
  console.log('   Press Ctrl+C to stop.');
  console.log('');

  // Keep process alive
  process.on('SIGINT', () => {
    console.log('\n👋 Shutting down...');
    process.exit(0);
  });

  process.on('SIGTERM', () => {
    console.log('\n👋 Shutting down...');
    process.exit(0);
  });
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
