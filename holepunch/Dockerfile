FROM node:22-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY relay.mjs ./
ENV NODE_ENV=production
ENV HOLEPUNCH_HOST=0.0.0.0
EXPOSE 8080
CMD ["node", "relay.mjs"]
