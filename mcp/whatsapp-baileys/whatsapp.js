/**
 * WhatsApp client wrapper using Baileys.
 * Handles connection, authentication, and message sending.
 */

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  jidNormalizedUser,
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

    // Owner identity - PAAW only responds to the owner's self-chat.
    // Captured on connection. Env vars are optional overrides.
    // NOTE: WhatsApp delivers self-chat text via the LID JID, so capturing
    // the owner's LID is essential (the phone JID only carries receipts).
    this.ownerPhoneJid = null;  // e.g. 918555934326@s.whatsapp.net
    this.ownerLidJid = null;    // e.g. 226589623238805@lid
    this.ownerNumber = (process.env.OWNER_NUMBER || '').replace(/[^0-9]/g, '') || null;
    this.ownerLidNumber = (process.env.OWNER_LID || '').replace(/[^0-9]/g, '') || null;

    // Operating mode:
    //   'self_chat' (default): bot runs on YOUR number; responds only in your
    //                          Note-to-Self chat. Needs the echo guard.
    //   'group':               bot runs on YOUR number; responds only in a
    //                          dedicated group (default name "PAAW"). Clean
    //                          separate space, no second number. Echo guard on.
    //   'dedicated':           bot runs on a SEPARATE number (PAAW's own);
    //                          responds only when YOUR number messages it.
    //                          OWNER_NUMBER must be your personal number.
    this.mode = (process.env.WHATSAPP_MODE || 'self_chat').toLowerCase();

    // Group mode config
    this.groupName = process.env.PAAW_GROUP_NAME || 'PAAW';
    this.groupJidConfig = process.env.PAAW_GROUP_JID || null;
    this.groupJid = null;  // resolved on connect

    // Track IDs of messages WE sent, so we don't process our own replies
    // as new input (self-chat echo loop: PAAW's reply is also fromMe=true).
    this.sentMessageIds = new Set();
    this.maxSentIds = 200;
  }

  /**
   * Group mode: resolve the JID of the dedicated PAAW group.
   * Uses PAAW_GROUP_JID if set, else matches a group by name (PAAW_GROUP_NAME).
   */
  async resolveGroupJid() {
    if (this.groupJidConfig) {
      this.groupJid = this.groupJidConfig;
      return;
    }
    try {
      const groups = await this.socket.groupFetchAllParticipating();
      const target = this.groupName.toLowerCase();
      for (const [jid, g] of Object.entries(groups)) {
        if (g.subject?.toLowerCase() === target) {
          this.groupJid = jid;
          return;
        }
      }
      console.log(`   ⚠️ No group named "${this.groupName}" found. Create it, then restart.`);
    } catch (e) {
      console.error('Failed to resolve group JID:', e.message);
    }
  }

  /**
   * Dedicated mode: check the SENDER is the configured owner (your personal
   * number messaging PAAW's number). Relies on env OWNER_NUMBER / OWNER_LID.
   */
  isAllowedSender(jid) {
    if (!jid) return false;
    const normalized = jidNormalizedUser(jid);
    if (this.ownerNumber && normalized.startsWith(this.ownerNumber)) return true;
    if (this.ownerLidNumber && normalized.startsWith(this.ownerLidNumber)) return true;
    return false;
  }

  /** Remember a message we sent, bounded to avoid unbounded growth. */
  rememberSentId(id) {
    if (!id) return;
    this.sentMessageIds.add(id);
    if (this.sentMessageIds.size > this.maxSentIds) {
      // Drop the oldest entry (Set preserves insertion order)
      const oldest = this.sentMessageIds.values().next().value;
      this.sentMessageIds.delete(oldest);
    }
  }

  /**
   * Capture owner identity from socket + persisted credentials.
   * Called on connection open and on creds updates (LID can populate late).
   */
  captureOwnerIdentity() {
    const me = this.socket?.authState?.creds?.me;

    const phone = this.socket?.user?.id || me?.id;
    if (phone) this.ownerPhoneJid = jidNormalizedUser(phone);

    const lid = this.socket?.user?.lid || me?.lid;
    if (lid) this.ownerLidJid = jidNormalizedUser(lid);
  }

  /**
   * Check whether a chat JID is the owner's own self-chat (Note to Self).
   * PAAW must ONLY engage here - all other chats are ignored.
   */
  isOwnerSelfChat(jid) {
    if (!jid) return false;
    const normalized = jidNormalizedUser(jid);
    if (this.ownerPhoneJid && normalized === this.ownerPhoneJid) return true;
    if (this.ownerLidJid && normalized === this.ownerLidJid) return true;
    // Env overrides (covers cases where auto-detection misses the LID)
    if (this.ownerNumber && normalized.startsWith(this.ownerNumber)) return true;
    if (this.ownerLidNumber && normalized.startsWith(this.ownerLidNumber)) return true;
    return false;
  }

  /**
   * Stable JID to send replies to (owner's Note-to-Self chat).
   */
  get selfChatJid() {
    return this.ownerPhoneJid || null;
  }

  /**
   * Default destination for "notify the owner" (used by jobs via the bridge).
   * Resolves per mode: self-chat JID, the PAAW group, or the owner's number.
   */
  get defaultTarget() {
    if (this.mode === 'group') return this.groupJid || null;
    if (this.mode === 'dedicated') {
      return this.ownerNumber ? `${this.ownerNumber}@s.whatsapp.net` : null;
    }
    return this.selfChatJid;  // self_chat
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

        // Capture owner identity in both addressing forms (phone + LID)
        this.captureOwnerIdentity();

        console.log('✅ Connected to WhatsApp!');
        console.log(`   Phone: ${this.socket.user?.id?.split(':')[0] || 'Unknown'}`);
        console.log(`   Name: ${this.socket.user?.name || 'Unknown'}`);
        console.log(`   Mode: ${this.mode}`);
        if (this.mode === 'dedicated') {
          console.log(`   Allowed owner number: ${this.ownerNumber || '(OWNER_NUMBER not set!)'}`);
          console.log('   🔒 PAAW responds only to the owner number messaging this bot.');
        } else if (this.mode === 'group') {
          await this.resolveGroupJid();
          console.log(`   Group name: ${this.groupName}`);
          console.log(`   Group JID:  ${this.groupJid || 'NOT FOUND'}`);
          console.log(`   🔒 PAAW will ONLY respond in the "${this.groupName}" group.`);
        } else {
          console.log(`   Owner phone JID: ${this.ownerPhoneJid || 'Unknown'}`);
          console.log(`   Owner LID JID:   ${this.ownerLidJid || 'Unknown'}`);
          console.log('   🔒 PAAW will ONLY respond in your self-chat (Note to Self).');
        }
      }
    });

    // Save credentials on update + re-capture owner identity (LID may
    // only become available after app-state sync completes)
    this.socket.ev.on('creds.update', async () => {
      await saveCreds();
      this.captureOwnerIdentity();
    });

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

        // Always ignore broadcasts / status / newsletters
        if (jid === 'status@broadcast' || jid.endsWith('@broadcast') || jid.endsWith('@newsletter')) continue;

        // ACCESS CONTROL: PAAW is a PERSONAL assistant for the owner only.
        let replyJid;

        if (this.mode === 'dedicated') {
          // Bot runs on PAAW's own number. Ignore our own sends, and only
          // respond when the configured owner number messages us.
          if (msg.key.fromMe) continue;
          if (!this.isAllowedSender(jid)) continue;
          replyJid = jid;
        } else if (this.mode === 'group') {
          // Only engage in the dedicated PAAW group.
          if (!this.groupJid || jid !== this.groupJid) continue;

          // ECHO GUARD: skip PAAW's own replies (also fromMe in the group).
          if (msg.key.id && this.sentMessageIds.has(msg.key.id)) {
            this.sentMessageIds.delete(msg.key.id);
            continue;
          }
          replyJid = this.groupJid;
        } else {
          // self_chat: only engage in the owner's Note-to-Self chat.
          if (!this.isOwnerSelfChat(jid)) continue;

          // ECHO GUARD: skip messages PAAW itself sent (its replies land back
          // in the self-chat as fromMe=true and would otherwise re-trigger us).
          if (msg.key.id && this.sentMessageIds.has(msg.key.id)) {
            this.sentMessageIds.delete(msg.key.id);
            continue;
          }
          replyJid = this.selfChatJid || jid;
        }

        // Extract message text
        const text = msg.message?.conversation ||
                     msg.message?.extendedTextMessage?.text ||
                     msg.message?.imageMessage?.caption ||
                     msg.message?.videoMessage?.caption ||
                     '';

        if (!text) continue;

        const pushName = msg.pushName || 'Owner';

        console.log(`📩 Owner message: ${text.substring(0, 50)}...`);

        if (this.messageHandler) {
          try {
            await this.messageHandler(replyJid, text, pushName, false);
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

    const sent = await this.socket.sendMessage(jid, { text });
    // Record the sent message id so the echo guard ignores it
    if (sent?.key?.id) {
      this.rememberSentId(sent.key.id);
    }
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
