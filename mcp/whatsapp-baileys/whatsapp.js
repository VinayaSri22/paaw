/**
 * WhatsApp client wrapper using Baileys.
 * Handles connection, authentication, and message sending.
 */

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import QRCode from 'qrcode';
import { mkdir } from 'fs/promises';
import { existsSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AUTH_DIR = path.join(__dirname, 'auth');
const QR_IMAGE_PATH = path.join(AUTH_DIR, 'qr.png');

const logger = pino({ level: 'warn' });

class WhatsAppClient {
  constructor() {
    this.socket = null;
    this.isConnected = false;
    this.messageHandler = null;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 5;
  }

  /**
   * Connect to WhatsApp.
   * @param {Function} onMessage - Callback for incoming messages (jid, message, pushName)
   */
  async connect(onMessage = null) {
    this.messageHandler = onMessage;

    // Ensure auth directory exists
    if (!existsSync(AUTH_DIR)) {
      await mkdir(AUTH_DIR, { recursive: true });
    }

    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    console.log('🔌 Connecting to WhatsApp...');
    console.log(`   Baileys version: ${version.join('.')}`);

    this.socket = makeWASocket({
      version,
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      printQRInTerminal: false, // We'll handle QR ourselves
      logger,
      browser: ['PAAW', 'Chrome', '120.0.0'],
      syncFullHistory: false,
      markOnlineOnConnect: false, // Don't mark online to reduce traffic
      shouldIgnoreJid: (jid) => {
        // Ignore broadcast and newsletter messages
        return jid?.endsWith('@broadcast') || jid?.endsWith('@newsletter');
      },
      // Don't sync old messages - we only want new ones
      getMessage: async () => undefined,
    });

    // Handle connection updates
    this.socket.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        console.log('\n📱 Scan this QR code with WhatsApp:');
        console.log('   (Also saved to: mcp/whatsapp-baileys/auth/qr.png)\n');
        
        // Show in terminal
        qrcodeTerminal.generate(qr, { small: true });
        
        // Save as image file
        try {
          await QRCode.toFile(QR_IMAGE_PATH, qr, {
            width: 300,
            margin: 2,
          });
          console.log(`\n   QR image saved: ${QR_IMAGE_PATH}`);
        } catch (err) {
          console.error('   Failed to save QR image:', err.message);
        }
      }

      if (connection === 'close') {
        const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
        const shouldReconnect = reason !== DisconnectReason.loggedOut;

        console.log(`❌ Connection closed. Reason: ${DisconnectReason[reason] || reason}`);

        if (shouldReconnect && this.reconnectAttempts < this.maxReconnectAttempts) {
          this.reconnectAttempts++;
          console.log(`   Reconnecting (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})...`);
          setTimeout(() => this.connect(this.messageHandler), 3000);
        } else if (reason === DisconnectReason.loggedOut) {
          console.log('   Logged out. Delete auth/ folder and restart to re-authenticate.');
        }
      } else if (connection === 'open') {
        this.isConnected = true;
        this.reconnectAttempts = 0;
        console.log('✅ Connected to WhatsApp!');
        console.log(`   Phone: ${this.socket.user?.id?.split(':')[0] || 'Unknown'}`);
        console.log(`   Name: ${this.socket.user?.name || 'Unknown'}`);
      }
    });

    // Save credentials on update
    this.socket.ev.on('creds.update', saveCreds);

    // Handle incoming messages
    this.socket.ev.on('messages.upsert', async ({ messages, type }) => {
      // DEBUG: log raw envelope of everything that arrives (before filtering)
      for (const m of messages) {
        const hasText = !!(m.message?.conversation || m.message?.extendedTextMessage?.text);
        console.log(
          `🔎 upsert type=${type} jid=${m.key?.remoteJid} fromMe=${m.key?.fromMe} ` +
          `hasMessage=${!!m.message} hasText=${hasText} keys=${m.message ? Object.keys(m.message).join(',') : 'none'}`
        );
      }

      // Process both notify (real-time) and append (some client deliveries)
      if (type !== 'notify' && type !== 'append') return;

      for (const msg of messages) {
        const jid = msg.key.remoteJid;
        if (!jid) continue;

        // Skip broadcasts / status / newsletters (but allow @lid - modern WA uses it)
        if (jid === 'status@broadcast' || jid.endsWith('@broadcast') || jid.endsWith('@newsletter')) continue;

        // Skip our own outgoing messages, EXCEPT self-chat (messaging your own number)
        const selfJid = this.socket.user?.id?.split(':')[0];
        const isSelfChat = selfJid && jid.startsWith(selfJid);
        if (msg.key.fromMe && !isSelfChat) continue;

        // Extract message text
        const text = msg.message?.conversation ||
                     msg.message?.extendedTextMessage?.text ||
                     msg.message?.imageMessage?.caption ||
                     msg.message?.videoMessage?.caption ||
                     '';

        if (!text) continue;

        const pushName = msg.pushName || 'Unknown';
        const isGroup = jid.endsWith('@g.us');

        console.log(`📩 Message from ${pushName} (${isGroup ? 'group' : 'private'}): ${text.substring(0, 50)}...`);

        if (this.messageHandler) {
          try {
            await this.messageHandler(jid, text, pushName, isGroup);
          } catch (err) {
            console.error('Error in message handler:', err);
          }
        }
      }
    });

    return this.socket;
  }

  /**
   * Send a text message.
   * @param {string} jid - WhatsApp JID (phone@s.whatsapp.net or groupid@g.us)
   * @param {string} text - Message text
   */
  async sendMessage(jid, text) {
    if (!this.socket || !this.isConnected) {
      throw new Error('WhatsApp not connected');
    }

    // Ensure JID format
    if (!jid.includes('@')) {
      jid = `${jid.replace(/[^0-9]/g, '')}@s.whatsapp.net`;
    }

    await this.socket.sendMessage(jid, { text });
    console.log(`📤 Sent message to ${jid}`);
  }

  /**
   * Send message to a phone number.
   * @param {string} phone - Phone number (with country code, no +)
   * @param {string} text - Message text
   */
  async sendToPhone(phone, text) {
    const cleanPhone = phone.replace(/[^0-9]/g, '');
    const jid = `${cleanPhone}@s.whatsapp.net`;
    await this.sendMessage(jid, text);
  }

  /**
   * Send message to a group by name (partial match).
   * @param {string} groupName - Group name to search for
   * @param {string} text - Message text
   * @returns {Object} - { success, groupName, jid } or { error }
   */
  async sendToGroup(groupName, text) {
    if (!this.socket || !this.isConnected) {
      throw new Error('WhatsApp not connected');
    }

    // Get all groups
    const groups = await this.socket.groupFetchAllParticipating();
    const groupEntries = Object.entries(groups);

    // Find group by partial name match (case insensitive)
    const searchLower = groupName.toLowerCase();
    const match = groupEntries.find(([_, group]) =>
      group.subject.toLowerCase().includes(searchLower)
    );

    if (!match) {
      return { error: `Group not found: ${groupName}` };
    }

    const [jid, group] = match;
    await this.sendMessage(jid, text);
    
    return { success: true, groupName: group.subject, jid };
  }

  /**
   * List all groups.
   * @returns {Array} - Array of { jid, name, participantCount }
   */
  async listGroups() {
    if (!this.socket || !this.isConnected) {
      throw new Error('WhatsApp not connected');
    }

    const groups = await this.socket.groupFetchAllParticipating();
    
    return Object.entries(groups).map(([jid, group]) => ({
      jid,
      name: group.subject,
      participantCount: group.participants?.length || 0,
    }));
  }

  /**
   * Check if connected.
   */
  get connected() {
    return this.isConnected;
  }
}

// Singleton instance
let clientInstance = null;

export function getWhatsAppClient() {
  if (!clientInstance) {
    clientInstance = new WhatsAppClient();
  }
  return clientInstance;
}

export { WhatsAppClient };
