FROM node:22.15.0-alpine
WORKDIR /app
COPY apps/prototype/package.json apps/prototype/package-lock.json ./apps/prototype/
RUN npm ci --prefix apps/prototype
COPY apps/prototype ./apps/prototype
RUN npm --prefix apps/prototype run build && npm --prefix apps/prototype run validate
ENV PORT=4173
EXPOSE 4173
USER node
CMD ["npm", "--prefix", "apps/prototype", "run", "dev"]
