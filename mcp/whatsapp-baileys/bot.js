/**
 * WhatsApp Bot for PAAW.
 * 
 * Listens for incoming WhatsApp messages and forwards them to PAAW's API,
 * allowing you to chat with PAAW from your phone.
 * 
 * Usage:
 *   node bot.js
 * 
 * Environment:
 *   PAAW_URL - PAAW API URL (default: http://localhost:8080)
 */

import { getWhatsAppClient } from './whatsapp.js';

const PAAW_URL = process.env.PAAW_URL || 'http://localhost:8080';
const TIMEOUT_MS = 120000; // 2 minutes for LLM responses

console.log('🐾 PAAW WhatsApp Bot');
console.log(`   PAAW API: ${PAAW_URL}`);
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
        user_id: `whatsapp_${userId.replace('@s.whatsapp.net', '').replace('@g.us', '')}`,
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
      console.error(`PAAW API error: ${response.status}`);
      return null;
    }

    const data = await response.json();
    return data.response || data.message || null;
  } catch (err) {
    if (err.name === 'AbortError') {
      console.error('PAAW API timeout');
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
  // For groups, only respond if mentioned (you can customize this)
  // For now, respond to all messages (private and group)
  
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
    
    console.log(`✅ Responded to ${pushName}`);
  } else {
    console.log(`⚠️ No response from PAAW for ${pushName}`);
  }
}

/**
 * Main entry point.
 */
async function main() {
  const client = getWhatsAppClient();

  // Connect with message handler
  await client.connect(handleMessage);

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
