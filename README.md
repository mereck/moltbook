# moltbook

Sandboxed LLM agent that reads [moltbook.com](https://www.moltbook.com) and comments on it using **MonadGPT** — a Mistral 7B fine-tuned on 11,000 texts from 1400–1700 CE. Ask it about science and it cites Ptolemy. Ask about medicine and you get humoral theory.

The agent runs inside a locked-down Docker container. It cannot escape to the host.

## Quick start

```bash
git clone https://github.com/mereck/moltbook.git
cd moltbook
docker compose up --build
```

First run pulls MonadGPT (~4 GB). The model is cached in a Docker volume for subsequent runs.

## Architecture

```
                    ┌─────────────┐
  Internet ◄────────┤  firewall   │  iptables OUTPUT policy: DROP
  (moltbook.com     │  (Alpine)   │  + allow moltbook.com :80/443
   only)            └──────┬──────┘  + allow ollama :11434
                           │
              network_mode: service:firewall
                           │
                    ┌──────┴──────┐        ┌──────────────┐
                    │   agent     │───────►│    ollama     │
                    │  (Python)   │  :11434│  (MonadGPT)   │
                    └─────────────┘        └──────────────┘
```

## Security layers

| Layer | Detail |
|---|---|
| **iptables firewall** | Default policy DROP. Only moltbook.com (80/443) and the Ollama sidecar (11434) are allowed. Configurable via `firewall/allowed_hosts.txt`. |
| **seccomp** | Syscall whitelist (~75 calls). Blocks ptrace, mount, chroot, bpf, io_uring, kernel modules, namespace escapes. |
| **Capabilities** | `cap_drop: ALL` on every container. |
| **Read-only rootfs** | All containers run with `read_only: true`. Only `/tmp` (tmpfs, noexec, size-limited) is writable. |
| **Non-root** | Agent runs as an unprivileged user with no shell (`/sbin/nologin`). |
| **No privilege escalation** | `no-new-privileges` on every container. |
| **Resource limits** | Agent: 256 MB RAM, 0.5 CPU, 64 PIDs. Ollama: 6 GB RAM, 2 CPUs. |

## Configuring the firewall

Edit `firewall/allowed_hosts.txt` to allow or deny hosts, then restart:

```bash
docker compose restart firewall
```

IPs are resolved once at firewall startup. Restart the firewall container to pick up DNS changes.

## Known limitations

- **Ollama has internet access** on the Docker bridge network (needed to pull the model on first boot). The agent cannot exploit this — its own egress is controlled by iptables in a shared network namespace.
- **DNS is resolved once** at firewall startup. If a host rotates IPs, restart the firewall container.
- **Ollama runs as root** inside its container. Mitigated by `cap_drop: ALL`, `no-new-privileges`, and `read_only`.
