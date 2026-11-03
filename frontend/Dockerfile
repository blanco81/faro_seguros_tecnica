# Etapa de build en glibc (Debian): las dependencias nativas del lockfile
# (rollup/esbuild) se resuelven igual que en desarrollo; evita sorpresas de
# musl/alpine y node 20 (EOL abril 2026). Multi-stage: el runtime es nginx.
FROM node:24-bookworm-slim AS build
WORKDIR /code
COPY package.json package-lock.json ./
# --include=dev blinda la instalacion frente a configuraciones omit=dev
# (.npmrc o NODE_ENV=production) que dejarian a vite sin instalar.
RUN npm ci --include=dev
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /code/dist /usr/share/nginx/html
EXPOSE 80
