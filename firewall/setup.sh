#!/bin/sh
set -e

ALLOWED_FILE="/etc/firewall/allowed_hosts.txt"

echo "[firewall] configuring iptables egress rules..."

# ── Flush ──
iptables -F OUTPUT

# ── Always allow ──
iptables -A OUTPUT -o lo -j ACCEPT                                   # loopback
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT  # replies

# ── Allow Docker DNS (127.0.0.11) ──
iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT

# ── Allow Ollama container (port 11434) ──
# Resolve the 'ollama' hostname to its container IP
OLLAMA_IP=$(getent hosts ollama 2>/dev/null | awk '{print $1}' | head -1)
if [ -n "$OLLAMA_IP" ]; then
    echo "[firewall] allowing ollama: $OLLAMA_IP:11434"
    iptables -A OUTPUT -d "$OLLAMA_IP" -p tcp --dport 11434 -j ACCEPT
else
    echo "[firewall] WARNING: could not resolve 'ollama' — LLM access blocked"
fi

# ── Allow external hosts from allowlist (ports 80/443 only) ──
while IFS= read -r host; do
    host=$(echo "$host" | sed 's/#.*//' | tr -d '[:space:]')
    [ -z "$host" ] && continue

    echo "[firewall] allowing: $host"
    for ip in $(getent hosts "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
        echo "[firewall]   -> $ip"
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
        iptables -A OUTPUT -d "$ip" -p tcp --dport 80  -j ACCEPT
    done
done < "$ALLOWED_FILE"

# ── Drop everything else ──
iptables -A OUTPUT -j DROP

echo ""
echo "[firewall] OUTPUT rules:"
iptables -L OUTPUT -n -v
echo ""
echo "[firewall] running — Ctrl+C to stop"
exec sleep infinity
