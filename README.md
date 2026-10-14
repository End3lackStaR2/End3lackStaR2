# End3lackStaR2

Utility workspace. Current contents: **`hermes/`** — a repair + verification kit for the
HermesAI OBS Kick notifier (`D:\HermesAI\OBS`).

## What is in here

| Path | Purpose |
| --- | --- |
| `hermes/hermes.ps1` | One-shot repair/verify tool (Windows PowerShell 5.1+, no python needed): finds the real config/log/python/launch command, tests both Pusher app keys live over a WebSocket, writes `kick_pusher.json`, patches `config.json`, restarts the notifier, then proves success from **new** log bytes only and writes EN/TH reports. |
| `hermes/hermes.cmd`, `hermes/selfcheck.cmd` | Double-click launchers for `fix` and for the self tests. |
| `hermes/tools/kick_pusher_probe.py` | Standard-library Pusher/WebSocket probe (no pip install) with a loopback end-to-end self test. |
| `hermes/tools/patch_kick_app.py` | Data + code patcher: creates `backend/config/pusher_config.py`, makes the Kick socket accept several keys and rotate automatically on `pusher:error` 4001/4003/4009. Backups + `--dry-run` + `--revert`. |
| `hermes/tools/test_patch_kick_app.py` | 10 unit tests that import the patched code and assert the key really rotates. |
| `hermes/README.md` | Thai quick start, all modes, troubleshooting. |
| `hermes/HERMES-BRIEF-TH.md` | Thai work order + acceptance criteria for an agent (no "wait 10-15 seconds" hand-waving). |

## Why

Kick rotated its public Pusher app key on 2026-10-08 (old `32cbd69e4b950bf97679` revoked,
new `34bf7a0ff419a2a775b9` on cluster `us2`, announced at https://x.com/notjoker).
Clients that still send the old key get `pusher:error code=4001 App key ... not in this
cluster`, the socket closes, the worker reconnects forever, and chat never appears — even
though the OAuth token is still fresh.

## Quick start (Windows)

```powershell
# branch URL works right now; after this is merged into main, swap the branch for 'main'
$u = 'https://raw.githubusercontent.com/End3lackStaR2/End3lackStaR2/arena/a508b692-end3lackstar2/hermes'
$d = "$env:USERPROFILE\hermes-kick"; New-Item -ItemType Directory -Force -Path $d | Out-Null
iwr -UseBasicParsing "$u/hermes.ps1" -OutFile "$d\hermes.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" doctor   # look, change nothing
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" fix      # repair + verify
```

## Verify the tooling itself (works offline)

```bash
python3 hermes/tools/kick_pusher_probe.py --selftest     # 9 checks incl. a loopback websocket server
cd hermes/tools && python3 -m unittest test_patch_kick_app -v   # 10 checks
```
