/**
 * WhatsApp MCP Server for PAAW.
 * 
 * Provides tools for sending WhatsApp messages from scheduled jobs.
 * 
 * Tools:
 *   - send_message: Send to a phone number
 *   - send_to_group: Send to a group by name
 *   - list_groups: List available groups
 * 
 * Usage:
 *   node index.js
 *   (Communicates via stdio JSON-RPC for MCP protocol)
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { getWhatsAppClient } from './whatsapp.js';

const server = new Server(
  {
    name: 'whatsapp-baileys',
    version: '1.0.0',
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// Tool definitions
const TOOLS = [
  {
    name: 'send_message',
    description: 'Send a WhatsApp message to a phone number. Use the phone number with country code (e.g., 919876543210 for India +91).',
    inputSchema: {
      type: 'object',
      properties: {
        phone: {
          type: 'string',
          description: 'Phone number with country code, no + sign (e.g., 919876543210)',
        },
        message: {
          type: 'string',
          description: 'Message text to send',
        },
      },
      required: ['phone', 'message'],
    },
  },
  {
    name: 'send_to_group',
    description: 'Send a WhatsApp message to a group. The group name can be a partial match (case insensitive).',
    inputSchema: {
      type: 'object',
      properties: {
        group_name: {
          type: 'string',
          description: 'Group name (partial match supported)',
        },
        message: {
          type: 'string',
          description: 'Message text to send',
        },
      },
      required: ['group_name', 'message'],
    },
  },
  {
    name: 'list_groups',
    description: 'List all available WhatsApp groups. Use this to find the correct group name before sending.',
    inputSchema: {
      type: 'object',
      properties: {},
      required: [],
    },
  },
];

// Handle list tools request
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return { tools: TOOLS };
});

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const client = getWhatsAppClient();

  // Ensure connected
  if (!client.connected) {
    console.error('WhatsApp not connected, attempting to connect...');
    await client.connect();
    
    // Wait for connection (up to 30 seconds)
    let waited = 0;
    while (!client.connected && waited < 30000) {
      await new Promise(r => setTimeout(r, 1000));
      waited += 1000;
    }
    
    if (!client.connected) {
      return {
        content: [{ type: 'text', text: 'Error: WhatsApp not connected. Please scan QR code first.' }],
        isError: true,
      };
    }
  }

  try {
    switch (name) {
      case 'send_message': {
        const { phone, message } = args;
        await client.sendToPhone(phone, message);
        return {
          content: [{ type: 'text', text: `Message sent to ${phone}` }],
        };
      }

      case 'send_to_group': {
        const { group_name, message } = args;
        const result = await client.sendToGroup(group_name, message);
        
        if (result.error) {
          return {
            content: [{ type: 'text', text: `Error: ${result.error}` }],
            isError: true,
          };
        }
        
        return {
          content: [{ type: 'text', text: `Message sent to group "${result.groupName}"` }],
        };
      }

      case 'list_groups': {
        const groups = await client.listGroups();
        
        if (groups.length === 0) {
          return {
            content: [{ type: 'text', text: 'No groups found.' }],
          };
        }
        
        const list = groups
          .map(g => `- ${g.name} (${g.participantCount} members)`)
          .join('\n');
        
        return {
          content: [{ type: 'text', text: `Available groups:\n${list}` }],
        };
      }

      default:
        return {
          content: [{ type: 'text', text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }
  } catch (err) {
    console.error(`Tool error (${name}):`, err);
    return {
      content: [{ type: 'text', text: `Error: ${err.message}` }],
      isError: true,
    };
  }
});

// Main entry point
async function main() {
  console.error('🐾 PAAW WhatsApp MCP Server starting...');
  
  // Initialize WhatsApp connection
  const client = getWhatsAppClient();
  await client.connect();
  
  // Start MCP server
  const transport = new StdioServerTransport();
  await server.connect(transport);
  
  console.error('✅ MCP server running');
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
