# link2post 运行镜像：Node 22 + Chromium + 中文字体
FROM node:22-slim

# 使用 Debian Chromium，兼容 amd64 与 arm64（Apple Silicon）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates chromium fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# puppeteer-core 使用系统 Chromium
ENV CHROME_PATH=/usr/bin/chromium
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

WORKDIR /app

# 3) 先装依赖（利用 Docker 层缓存，源码变更不用重装）
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# 4) 拷贝源码
COPY . .

ENV NODE_ENV=production
ENV PORT=4780
ENV HOST=0.0.0.0

EXPOSE 4780

CMD ["node", "server.js"]
