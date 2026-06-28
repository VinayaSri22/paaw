# WhatsApp Bot/MCP Server using Baileys
FROM node:20-alpine

WORKDIR /app

# Install dependencies for Baileys
RUN apk add --no-cache \
    python3 \
    make \
    g++ \
    git

# Copy package files
COPY package.json ./

# Install dependencies
RUN npm install --production

# Copy source files
COPY *.js ./

# Auth directory will be mounted as volume
VOLUME /app/auth

# Default command runs the chat bot
# For MCP mode, override with: node index.js
CMD ["node", "bot.js"]
