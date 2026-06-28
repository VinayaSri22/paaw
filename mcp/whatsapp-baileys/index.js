/**
 * WhatsApp MCP Server for PAAW (HTTP bridge client).
 *
 * IMPORTANT: This MCP server does NOT open its own WhatsApp connection.
 * A second Baileys session sharing the same auth would corrupt the signal
 * sessions (Bad MAC errors). Instead, it forwards tool calls over HTTP to the
 * long-running WhatsApp bot (bot.js), which owns the single live connection.
 *
 * Tools (parity with the Discord MCP):
 *   - send_whatsapp_message : send to a phone number
 *   - send_whatsapp_to_group: send to a group by name
 *   - send_whatsapp_to_me   : send to the owner (default target for the mode)
 *   - list_whatsapp_groups  : list available groups
 *
 * Usage (spawned by PAAW's job executor over stdio):
 *   node index.js
 *
 * Environment:
 *   WHATSAPP_BRIDGE_URL - URL of the bot.js HTTP bridge
 *                         (default: http://host.docker.internal:3000)
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

const BRIDGE_URL = (process.env.WHATSAPP_BRIDGE_URL || 'http://host.docker.internal:3000').replace(/\/$/, '');

const server = new Server(
  { name: 'whatsapp', version: '2.0.0' },
  { capabilities: { tools: {} } }
);

const TOOLS = [
  {
    name: 'send_whatsapp_message',
    description: 'Send a WhatsApp message to a phone number (country code, no + sign, e.g. 919876543210).',
    inputSchema: {
      type: 'object',
      properties: {
        phone: { type: 'string', description: 'Phone number with country code, no + (e.g. 919876543210)' },
        message: { type: 'string', description: 'Message text to send' },
      },
      required: ['phone', 'message'],
    },
  },
  {
    name: 'send_whatsapp_to_group',
    description: 'Send a WhatsApp message to a group by name (partial, case-insensitive match).',
    inputSchema: {
      type: 'object',
      properties: {
        group_name: { type: 'string', description: 'Group name (partial match supported)' },
        message: { type: 'string', description: 'Message text to send' },
      },
      required: ['group_name', 'message'],
    },
  },
  {
    name: 'send_whatsapp_to_me',
    description: 'Send a WhatsApp message to the owner (PAAW user). Delivers to the configured chat for the active mode (Note-to-Self, the PAAW group, or your number). Use this for job notifications.',
    inputSchema: {
      type: 'object',
      properties: {
        message: { type: 'string', description: 'Message text to send to the owner' },
      },
      required: ['message'],
    },
  },
  {
    name: 'list_whatsapp_groups',
    description: 'List available WhatsApp groups. Use to find the correct group name before sending.',
    inputSchema: { type: 'object', properties: {}, required: [] },
  },
];

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

async function bridgePost(path, body) {
  const res = await fetch(`${BRIDGE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Bridge error ${res.status}`);
  return data;
}

async function bridgeGet(path) {
  const res = await fetch(`${BRIDGE_URL}${path}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Bridge error ${res.status}`);
  return data;
}

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case 'send_whatsapp_message': {
        await bridgePost('/send', { phone: args.phone, message: args.message });
        return { content: [{ type: 'text', text: `Message sent to ${args.phone}` }] };
      }

      case 'send_whatsapp_to_group': {
        const result = await bridgePost('/send', { group: args.group_name, message: args.message });
        return { content: [{ type: 'text', text: `Message sent to group "${result.groupName || args.group_name}"` }] };
      }

      case 'send_whatsapp_to_me': {
        const result = await bridgePost('/send', { message: args.message });
        return { content: [{ type: 'text', text: `Message sent to owner (${result.to || 'default target'})` }] };
      }

      case 'list_whatsapp_groups': {
        const { groups = [] } = await bridgeGet('/groups');
        if (groups.length === 0) {
          return { content: [{ type: 'text', text: 'No groups found.' }] };
        }
        const list = groups.map((g) => `- ${g.name} (${g.participantCount} members)`).join('\n');
        return { content: [{ type: 'text', text: `Available groups:\n${list}` }] };
      }

      default:
        return { content: [{ type: 'text', text: `Unknown tool: ${name}` }], isError: true };
    }
  } catch (err) {
    console.error(`Tool error (${name}):`, err.message);
    return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
  }
});

async function main() {
  console.error('🐾 PAAW WhatsApp MCP Server (bridge client)');
  console.error(`   Bridge: ${BRIDGE_URL}`);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('✅ MCP server running');
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
