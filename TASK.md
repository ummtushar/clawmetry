# TASK: Fix sync daemon + cloud dashboard for System Health, Active Tasks, and realtime sync

## Context
ClawMetry is an open-source observability dashboard for OpenClaw AI agents. It has:
- A local dashboard (dashboard.py, published to PyPI as `clawmetry`)
- A sync daemon (packages/clawmetry/sync_patched.py) that ships encrypted data to cloud
- A cloud dashboard (on another machine at ~/projects/clawmetry-cloud/dashboard.py, accessible via SSH vivek@192.168.178.57)

The sync daemon runs on user machines, collects system info + session data, encrypts it, and POSTs to the cloud. The cloud stores blobs in Turso DB. The cloud dashboard has JS overlay scripts that intercept local API calls (/api/system-health, /api/subagents, /api/overview) and instead fetch+decrypt the encrypted blobs.

## Files to edit

### 1. OSS sync daemon: `packages/clawmetry/sync_patched.py`
This is the source that gets published to PyPI. The installed copy is at `~/.clawmetry/lib/python3.9/site-packages/clawmetry/sync.py` (1005 lines, has local hacks that need to be ported properly).

### 2. Cloud dashboard: SSH to vivek@192.168.178.57, file at ~/projects/clawmetry-cloud/dashboard.py
This is a ~16000 line Flask app. The JS overlay scripts are embedded as Python string literals around lines 9850-9920.

## Bugs to fix

### Bug 1: sync_logs hangs on large log files (CRITICAL)
**Problem:** `sync_logs()` tries to read entire log files. On machines with large logs (e.g., 149MB), this blocks the sync loop forever. The daemon never reaches `save_state()`, so it redoes initial sync every restart, and `sync_system_snapshot()` never runs in the main loop.

**Fix in sync_patched.py:**
- In `sync_logs()`, cap the number of lines read per cycle (e.g., max 1000 lines from the tail)
- Or better: track file position in state and only read new lines (tail-follow pattern)
- The installed version has a hack that just skips sync_logs entirely - we need a proper fix

### Bug 2: Cron path resolution wrong
**Problem:** `sync_system_snapshot()` looks for crons at `os.path.join(paths.get("workspace", ""), "..", "crons.json")` which doesn't exist. Actual cron file is at `~/.openclaw/cron/jobs.json` and it's a JSON object with a "jobs" array, not a flat array.

**Fix in sync_patched.py:**
- Check multiple candidate paths: `~/.openclaw/cron/jobs.json`, `~/.openclaw/agents/main/cron/jobs.json`, then the old path as fallback
- Handle both formats: `{"jobs": [...]}` (new) and `[...]` (old)

### Bug 3: Subagent data too sparse for Active Tasks widget
**Problem:** The subagent list in the system snapshot only has `label`, `status`, `model`, `task`, `tokens`. The Active Tasks widget needs `sessionId`, `key`, `displayName`, `runtimeMs`, `updatedAt`.

**Fix in sync_patched.py:**
- Add these fields to the subagent dict in `sync_system_snapshot()`:
  - `sessionId`: `key.split(":")[-1]`
  - `key`: the full session key
  - `displayName`: `meta.get("label", meta.get("task", ""))[:80]`
  - `updatedAt`: `meta.get("updatedAt", 0)`
  - `runtimeMs`: `int(now_ms - meta.get("createdAt", meta.get("updatedAt", now_ms)))`

### Bug 4: Cloud System Health overlay maps wrong fields
**Problem:** The JS overlay intercepting `/api/system-health` in cloud mode returns `{services: snap.services, disk: snap.disk, system: snap.system}` but:
- `snap.services` doesn't exist (services info is in `snap.system` array as `["Gateway", "Running", "green"]`)
- `snap.disk` doesn't exist (disk info is in `snap.system` array as `["Disk /", "15Gi / 926Gi (2%)", "green"]`)
- The widget expects `{services: [{name,up,port}], disks: [{mount,used_gb,total_gb,pct}], crons: {enabled,ok24h,failed}, subagents: {runs,successPct}}`

**Fix in cloud dashboard.py (SSH to Dhriti):**
- Parse `snap.system` array to extract Gateway -> services and Disk -> disks
- Map `snap.cronEnabled`/`snap.cronDisabled` to crons object
- Map `snap.subagentCounts` to subagents object
- Disk regex must handle multi-char suffixes like "Gi": use `\w*` not `\w?`

### Bug 5: Active Tasks shows "no active tasks" even when agents are running
**Problem:** `loadActiveTasks()` fetches `/api/subagents` and filters `status === 'active'`. The daemon marks agents as "active" only if updated <2 minutes ago. Agents that just finished show as "idle" immediately.

**Fix in cloud dashboard.py:**
- In the `/api/subagents` overlay, promote "idle" agents to "active" for display (they were recently active)
- Only "stale" agents (>1 hour old) should be excluded from Active Tasks

### Bug 6: Sub-Agents (24H) count wrong
**Problem:** System Health shows "1 Run" because it counts from `subagentCounts` which only has the current snapshot. Should show total subagent sessions in last 24h.

**Fix in cloud dashboard.py:**
- Use `(snap.subagents||[]).length` for total run count instead of just active count

### Bug 7: Brain tab not realtime
**Problem:** The Brain tab loads events once and doesn't auto-refresh. User expects it to update as new events come in.

**Fix in cloud dashboard.py:**
- The brain decrypt overlay already has a refresh timer (`window._brainRefreshTimer`) that calls `loadBrainPage(true)` every 8 seconds when the brain tab is active. Check if this is working in cloud mode - the overlay may be not applying correctly, or the timer may not be starting.

## How to test
1. After fixing sync_patched.py, copy it to the installed location: `cp packages/clawmetry/sync_patched.py ~/.clawmetry/lib/python3.9/site-packages/clawmetry/sync.py`
2. Restart the daemon: `pkill -f "clawmetry/sync.py"; sleep 2; python3 ~/.clawmetry/lib/python3.9/site-packages/clawmetry/sync.py &`
3. For cloud fixes, SSH to vivek@192.168.178.57 and edit ~/projects/clawmetry-cloud/dashboard.py
4. Deploy cloud: `ssh vivek@192.168.178.57 "cd ~/projects/clawmetry-cloud && bash deploy.sh"`
5. Check https://app.clawmetry.com/node/vivekchand19+demo2/?token=cm_c2057601e4df4edcb14d56d409ea1c38

## Important
- Do NOT change the encryption logic
- Do NOT change the /ingest/* endpoint contracts
- Keep backward compatibility - old daemons should still work with new cloud
- Test your regex against values like "15Gi / 926Gi (2%)" and "74G / 233G (33%)"
