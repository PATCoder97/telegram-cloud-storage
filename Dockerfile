FROM node:24-bookworm-slim AS frontend-build

WORKDIR /frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/index.html frontend/env.d.ts frontend/postcss.config.cjs frontend/tsconfig.json frontend/tsconfig.app.json frontend/tsconfig.node.json frontend/vite.config.ts ./
COPY frontend/public ./public
COPY frontend/src ./src
RUN pnpm install --frozen-lockfile
RUN pnpm run build

FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend_dist ./frontend_dist

EXPOSE 5010

CMD ["python", "app.py"]
