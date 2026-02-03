FROM alpine:3.20

RUN apk add --no-cache iptables

COPY setup.sh /usr/local/bin/firewall-setup.sh

ENTRYPOINT ["/usr/local/bin/firewall-setup.sh"]
