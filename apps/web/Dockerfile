FROM node:22.15.0-alpine AS dependencies
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./apps/web/
RUN npm ci --prefix apps/web

FROM dependencies AS test
COPY apps/web ./apps/web
USER node
CMD ["npm", "--prefix", "apps/web", "test"]

FROM dependencies AS builder
COPY apps/web ./apps/web
RUN npm --prefix apps/web run build \
    && npm --prefix apps/web run validate

FROM nginxinc/nginx-unprivileged:1.28.0-alpine AS runtime
COPY apps/web/nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/apps/web/dist/ /usr/share/nginx/html/
USER 101:101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
