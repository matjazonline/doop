# Trusted test infrastructure, separate from the non-root production image.
FROM docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS docker-cli
FROM oven/bun:1.3.10@sha256:b86c67b531d87b4db11470d9b2bd0c519b1976eee6fcd71634e73abfa6230d2e AS bun
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

# Reuse the reviewed signed package snapshot for the fixture dependencies.
RUN node -e "const fs=require('node:fs');fs.mkdirSync('/etc/ssl/certs',{recursive:true});fs.writeFileSync('/etc/ssl/certs/ca-certificates.crt',require('node:tls').rootCertificates.join('\n'))" \
 && rm -f /etc/apt/sources.list.d/debian.sources \
 && printf '%s\n' \
    'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261106T000000Z bookworm main' \
    'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261106T000000Z bookworm-updates main' \
    'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261106T000000Z bookworm-security main' \
    > /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-dejavu-core=2.37-6 unzip ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker-real
RUN bun --version && docker-real --version

WORKDIR /work
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --ignore-scripts
COPY .chrome-version ./
# Chrome for Testing has no Linux arm64 build. ARM uses Debian Chromium;
# x64 uses the committed CI version, and an unavailable pin fails the build.
RUN set -e; \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
      ./node_modules/.bin/browsers install "chrome@$(cat .chrome-version)" --path /opt/chrome > /tmp/chrome-install; \
      awk '{ print $2 }' /tmp/chrome-install > /opt/chrome-path; \
    else mkdir -p /opt; echo /usr/bin/chromium > /opt/chrome-path; fi; \
    "$(cat /opt/chrome-path)" --version
COPY . .
COPY scripts/test-linux-docker.sh /usr/local/bin/docker
RUN chmod 0755 /usr/local/bin/docker

# Root/no-sandbox applies only to this trusted Docker-enabled test runner.
ENV CI=true CHROME_NO_SANDBOX=1
ENTRYPOINT ["sh", "-c", "docker info >/dev/null && mkdir -p \"$TMPDIR\" && test ! -e \"$DOOP_TEST_WORKSPACE\" && cp -a /work \"$DOOP_TEST_WORKSPACE\" && cd \"$DOOP_TEST_WORKSPACE\" && CHROME_PATH=$(cat /opt/chrome-path) exec bun run test -- \"$@\"", "test"]
