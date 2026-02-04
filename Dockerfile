FROM python:3.12-alpine3.21 AS builder

RUN pip install --no-cache-dir --target=/deps \
    requests==2.32.3 \
    beautifulsoup4==4.12.3

FROM python:3.12-alpine3.21

# Remove tools an attacker could abuse
RUN apk --no-cache add tini \
    && rm -rf /usr/bin/wget /usr/bin/nc \
    && rm -rf /var/cache/apk/* /tmp/* \
    && find / -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

COPY --from=builder /deps /usr/local/lib/python3.12/site-packages

# Non-root user with no home dir and no shell
RUN addgroup -S sandbox && adduser -S -G sandbox -H -s /sbin/nologin agent

WORKDIR /app
COPY --chown=agent:sandbox agent.py .

USER agent

ENTRYPOINT ["tini", "--"]
CMD ["python", "-u", "agent.py"]
