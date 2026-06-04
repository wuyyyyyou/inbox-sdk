# Diagnose WSL Gmail API Connectivity

This skill diagnoses why Gmail API cannot be reached from WSL and fixes the proxy configuration in `dev-wsl.sh`.

---

## Trigger

User reports: Brief scan returns 0 emails, Gmail API timeout, "Name or service not known", "scan_fallback_empty", or general WSL network issues accessing Google services.

---

## Step 1 — Check basic WSL internet access

Run in user's WSL terminal:

```bash
# Chinese sites reachable?
curl -s -o /dev/null -w "HTTP %{http_code} · %{time_total}s\n" --connect-timeout 3 "https://www.baidu.com"

# Google reachable?
curl -s -o /dev/null -w "HTTP %{http_code} · %{time_total}s\n" --connect-timeout 3 "https://gmail.googleapis.com"
```

| Result | Meaning |
|---|---|
| Baidu OK, Google timeout | GFW blocking Google — need proxy |
| Both timeout | No internet at all — check WSL network adapter |
| Both OK | Proxy already working, problem is elsewhere (token, code bug, etc.) |

---

## Step 2 — Find Windows host IP

WSL2's Windows host IP can be at different addresses depending on network mode. Check **both**:

```bash
# Primary: default route gateway (most reliable for bridged/NAT WSL2)
ip route show default | awk '{print $3; exit}'

# Fallback: /etc/resolv.conf nameserver (works for mirrored-mode WSL2)
awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf
```

If these return different IPs, the gateway (`ip route`) is usually the correct one. `10.255.255.254` is a DNS relay, NOT the host.

---

## Step 3 — Test proxy reachability

Default Clash port is 7890. Replace `<HOST_IP>` with the IP from Step 2:

```bash
# Test the proxy port directly
curl -s -o /dev/null -w "HTTP %{http_code} · %{time_total}s\n" --connect-timeout 3 "http://<HOST_IP>:7890"
```

| Result | Meaning |
|---|---|
| HTTP 400 | Proxy is alive — port open, Clash responding |
| HTTP 000 + fast timeout | Port closed — Clash not running, wrong port, or Windows firewall blocking |
| HTTP 000 + slow timeout | Network unreachable — IP might be wrong |

If port is unreachable, ask user to check:
- Is Clash running? (tray icon on Windows)
- Is the port correct? (Clash → Settings → Mixed Port, could be 7890, 7897, 1080, etc.)
- Windows Firewall: allow inbound from WSL virtual network (`172.x.x.x` or `192.168.x.x`)

---

## Step 4 — Test Gmail API through proxy

```bash
curl -s -o /dev/null -w "HTTP %{http_code} · %{time_total}s\n" --connect-timeout 5 \
  -x "http://<HOST_IP>:7890" "https://gmail.googleapis.com"
```

| Result | Meaning |
|---|---|
| HTTP 404 | **SUCCESS** — reached Google servers (404 is expected, root path has no endpoint) |
| HTTP 401 | Also success — authenticated endpoint would need a token |
| HTTP 000 + timeout | Proxy failing to forward to Google — Clash rule issue or proxy chain broken |

---

## Step 5 — Fix `dev-wsl.sh` proxy logic

The file is at `anna-inbox/dev-wsl.sh`. The proxy section should:

1. **Priority order**: `ip route` gateway first, then `/etc/resolv.conf` nameserver
2. **Verify proxy is alive** before setting env vars (curl check)
3. **Set both uppercase and lowercase** env vars (`HTTPS_PROXY` + `https_proxy`)
4. **Print status** to stderr so user knows if proxy was enabled

Correct proxy section:

```bash
# WSL proxy — 通过 Windows 宿主机 Clash 代理访问外网（Google API 等）
# 宿主机 IP 探测：先用 ip route 拿网关（最可靠），失败再用 /etc/resolv.conf nameserver
HOST_IP=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
if [ -z "${HOST_IP:-}" ] && [ -f /etc/resolv.conf ]; then
  HOST_IP=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf 2>/dev/null)
fi

PROXY_PORT="${ANNA_PROXY_PORT:-7890}"
if [ -n "${HOST_IP:-}" ] && curl -s --connect-timeout 1 --max-time 2 "http://$HOST_IP:$PROXY_PORT" >/dev/null 2>&1; then
  export HTTP_PROXY="http://$HOST_IP:$PROXY_PORT"
  export HTTPS_PROXY="http://$HOST_IP:$PROXY_PORT"
  export http_proxy="http://$HOST_IP:$PROXY_PORT"
  export https_proxy="http://$HOST_IP:$PROXY_PORT"
  export NO_PROXY="localhost,127.0.0.1,::1"
  export no_proxy="localhost,127.0.0.1,::1"
  echo "[dev-wsl] proxy enabled: $HOST_IP:$PROXY_PORT" >&2
else
  echo "[dev-wsl] proxy skipped (host=$HOST_IP port=$PROXY_PORT unreachable)" >&2
fi
```

---

## Step 6 — Verify fix

Restart `bash dev-wsl.sh`. Look for first line:
- `[dev-wsl] proxy enabled: 172.x.x.x:7890` — fixed
- `[dev-wsl] proxy skipped (...)` — still broken, re-check Steps 2-3

Then trigger a Brief scan and check bridge logs for `fallback to cache` messages. With proxy working, no fallback should occur.

---

## Common failure patterns

| Symptom | Root Cause | Fix |
|---|---|---|
| `[Errno -2] Name or service not known` | DNS via proxy failed | Check Clash DNS settings |
| `timeout` after 15s per thread | Proxy down or wrong IP | Steps 2-3 |
| Chinese sites OK, Google blocked | No proxy / proxy not working | Steps 3-5 |
| `nameserver` is `10.255.255.254` | WSL NAT mode DNS relay | Use `ip route` gateway instead |
| `scan_fallback_empty` in RPC log | Gmail API unreachable AND local cache deleted | Fix proxy, then re-run scan |
