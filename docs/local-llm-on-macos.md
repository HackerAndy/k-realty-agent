# Local LLM on macOS: "No route to host" when the server is up

**Symptom.** The harness can't reach a local/LAN OpenAI-compatible LLM server, and
fails with:

```
could not reach http://192.168.0.120:9090/v1/models: [Errno 65] No route to host
```

…while `curl` reaches the *same* URL from the *same* shell perfectly well:

```
$ curl -s -m 5 -o /dev/null -w "curl=%{http_code}\n" http://192.168.0.120:9090/v1/models
curl=401          # 401 = reachable; auth is a separate matter
```

That combination — curl fine, Python `EHOSTUNREACH` — is the whole diagnosis. It
is **not** the network, DNS, the base URL, the API key, or IPv6 ordering.

## Cause: macOS Local Network privacy, per binary

Since macOS 15, reaching a device on the local network requires that permission
be granted to the *executable making the connection*. When it's denied, macOS
does not return a distinct "permission denied" — it returns **EHOSTUNREACH
(errno 65)**, which reads exactly like a dead server. That's what sends you
debugging the wrong layer.

`/usr/bin/curl` is Apple-signed and passes. A Homebrew Python does not:

```
$ codesign -dv --verbose=2 "$(readlink -f .venv/bin/python)"
Identifier=python3-55554944ce5ef0398b31356b803d54135dc096ed
TeamIdentifier=not set          # ad-hoc signed
```

`TeamIdentifier=not set` means there is no stable developer identity to attach a
grant to, so the permission frequently never prompts and stays silently denied.

### Why "just grant the permission" is not a durable fix here

Homebrew installs each version at its own path and does not replace binaries on
upgrade:

```
/opt/homebrew/Cellar/python@3.14/3.14.0_1/...
/opt/homebrew/Cellar/python@3.14/3.14.4_1/...   ← both present
```

A new path is a new binary is a new identity, so **every `brew upgrade` drops the
grant**, as does switching interpreters (3.12 / 3.13 / 3.14, python.org, pyenv).

One thing it does *not* multiply over: virtualenvs. `.venv/bin/python` is a
symlink to a base interpreter, so every venv and poetry env sharing that
interpreter shares one grant. The churn that breaks this is **version** churn,
not environment churn.

Grant the permission only if you pin a single interpreter. Otherwise use one of
the fixes below.

## Fix 1 (recommended): reach the server over loopback or a VPN address

Local Network privacy does not gate `127.0.0.1`, and generally does not gate
traffic over a VPN `utun` interface. Either sidesteps the problem for **every**
interpreter, permanently.

- **Tailscale / WireGuard** — best fit if you change Python versions often or
  want the model reachable off-LAN. Runs as a system service (survives restarts
  on its own), stable hostname, nothing to re-grant. Verify with the one-liner in
  [Diagnosing](#diagnosing) against the `100.x` address before relying on it.
- **SSH tunnel to loopback** — no new software; see Fix 2.

## Fix 2: persistent SSH tunnel (launchd)

Forward a local port to the LLM server, then point the harness at
`http://127.0.0.1:9090/v1`.

Prerequisites on the target Mac: Remote Login enabled, and your public key in its
`~/.ssh/authorized_keys`. Confirm passwordless auth first — this must print `ok`
with no prompt:

```bash
ssh -o BatchMode=yes drew@192.168.0.120 echo ok
```

Create `~/Library/LaunchAgents/com.krealty.llm-tunnel.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.krealty.llm-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>-T</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>StrictHostKeyChecking=accept-new</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-i</string><string>/Users/drew/.ssh/id_ed25519</string>
    <string>-L</string><string>9090:127.0.0.1:9090</string>
    <string>drew@192.168.0.120</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>/tmp/krealty-llm-tunnel.err</string>
</dict>
</plist>
```

Load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.krealty.llm-tunnel.plist
```

Then set **Settings → LLM provider → Base URL** to `http://127.0.0.1:9090/v1`.

Manage it with:

```bash
launchctl print gui/$(id -u)/com.krealty.llm-tunnel     # state
launchctl kickstart -k gui/$(id -u)/com.krealty.llm-tunnel   # restart
launchctl bootout gui/$(id -u)/com.krealty.llm-tunnel        # remove
```

### Why those flags

| Flag | Why it matters |
| --- | --- |
| `ExitOnForwardFailure=yes` | ssh **dies** if the forward can't bind. Without it launchd sees a healthy process wrapping a dead tunnel. |
| `ServerAliveInterval=30`, `ServerAliveCountMax=3` | Tears down a half-open connection after ~90s so `KeepAlive` can respawn it. |
| `ThrottleInterval=10` | Prevents a respawn storm while the other Mac is asleep. |
| `BatchMode=yes` | Never blocks waiting for a passphrase prompt nothing can answer. |

### Caveats

1. `-L 9090:127.0.0.1:9090` resolves `127.0.0.1` **on the remote side**. If the
   LLM server binds only its LAN address, use `-L 9090:192.168.0.120:9090`.
2. A LaunchAgent starts **at login**, not at boot. Boot-time needs a LaunchDaemon
   running as root with a root-readable key — more moving parts; not recommended
   for this.
3. launchd-spawned processes can be denied Local Network access *silently* (there
   is no UI to prompt at login). If the tunnel never comes up, read
   `/tmp/krealty-llm-tunnel.err` first.

## Diagnosing

Run this in the same shell you'd start the harness from. It puts curl and Python
side by side in one environment, which is what makes the result conclusive:

```bash
env | grep -i proxy; curl -s -m 5 -o /dev/null -w "curl=%{http_code}\n" http://192.168.0.120:9090/v1/models; poetry run python -c "
from urllib import request
try: print('python=', request.urlopen('http://192.168.0.120:9090/v1/models', timeout=5).status)
except Exception as e: print('python FAILED:', type(e).__name__, e)"
```

| Result | Meaning |
| --- | --- |
| curl ok, python FAILED | This issue (or a proxy var — check the `env` output). |
| both ok | This shell is fine; whatever you were clicking ran somewhere else. |
| both fail | No LAN route from this shell at all; the working curl came from another context. |

Note that a **sandboxed** shell (e.g. a tool-runner's shell) may have no LAN route
regardless of TCC, so it cannot be used to test this. Use a real terminal.

## How the harness reports it

`core/tools/llm_provider.list_models()` detects this specific shape — darwin, a
private/`.local` host, and `EHOSTUNREACH` — and emits a
[logging-standard](../core/observability.py) record with code
**`LLM_LOCAL_NETWORK_BLOCKED`**, naming the interpreter that needs the grant:

```json
{
  "code": "LLM_LOCAL_NETWORK_BLOCKED",
  "component": "core.tools.llm_provider",
  "context": { "host": "192.168.0.120", "interpreter": ".venv/bin/python", "platform": "darwin" },
  "message": "macOS blocked this Python process from reaching 192.168.0.120 on the local network …",
  "remediation": "Grant Local Network access … or point the harness at a loopback tunnel …"
}
```

Deliberately narrow, so it never mislabels a genuinely dead server:

| Code | Condition |
| --- | --- |
| `LLM_LOCAL_NETWORK_BLOCKED` | darwin + LAN host + `EHOSTUNREACH` |
| `LLM_SERVER_UNREACHABLE` | any other connection failure (e.g. `ECONNREFUSED` = really down) |
| `LLM_SERVER_HTTP_ERROR` | server answered (a 401 means the network is fine — check the key) |
| `LLM_MODELS_UNREADABLE` | answered, but `/models` wasn't valid JSON |

## Scope

macOS-only. There is no TCC on Linux, so this disappears entirely when the
harness runs on a Linux host (e.g. the Raspberry Pi deployment target).
