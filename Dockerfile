FROM node:22.14-alpine AS builder
WORKDIR /app

COPY . .
COPY .env.docker .env.local
RUN apk --no-cache add --virtual .builds-deps build-base python3
# Hard guard: prod builds must NOT carry the EXPOSE_DB=true debug surface
# (boot/expose-debug.ts would mount window.__db__ / window.__authSource__).
# If anyone leaks EXPOSE_DB=true into .env.docker, fail the build loudly.
RUN if grep -E '^EXPOSE_DB[[:space:]]*=[[:space:]]*true' .env.local >/dev/null 2>&1; then \
      echo "ERROR: EXPOSE_DB=true present in .env.docker — refusing to build prod image" >&2; \
      exit 1; \
    fi
RUN npm install -g pnpm
RUN pnpm install && pnpm build -m pwa

FROM python:3.12.7-slim
WORKDIR /app

COPY src-backend/ .
COPY --from=builder /app/dist/pwa ./static
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 9010
# 启动时若 BACKEND_DATA_API_ENABLED=true 先跑 alembic upgrade head（幂等），
# 否则跳过——my-deploy 这类不开 backend data API 的部署没有 DATABASE_URL，
# 不能误跑 alembic。exec 让 uvicorn 替换 shell 进程，确保容器收到的 SIGTERM
# 能传到 uvicorn 触发优雅停机。
CMD ["sh", "-c", "if [ \"$BACKEND_DATA_API_ENABLED\" = \"true\" ]; then alembic upgrade head; fi && exec uvicorn app:app --host 0.0.0.0 --port 9010"]
