# P0 TASK: Fix Gateway Token Login + Reconcile OSS sync daemon

## Bug 1: Gateway Token Login Broken (P0 - affects ALL users)

### Root Cause
`_load_gw_config()` in dashboard.py reads `~/.clawmetry-gateway.json` FIRST:
```python
with open(_GW_CONFIG_FILE) as f:  # ~/.clawmetry-gateway.json
    cfg = json.load(f)
    GATEWAY_TOKEN = cfg.get('token', GATEWAY_TOKEN)
```

This file contains a STALE token that was saved during initial setup. But the user's REAL gateway token is in `~/.openclaw/openclaw.json` at `gateway.auth.token`.

When a user enters their real gateway token, `api_auth_check` compares it against the stale token from `~/.clawmetry-gateway.json`, which doesn't match. Login fails.

### Fix Required (in ~/clawmetry-dev/dashboard.py on THIS machine - Dhriti 192.168.178.57)
1. `_load_gw_config()` should ALWAYS check the live gateway config (`~/.openclaw/openclaw.json` -> `gateway.auth.token`) as the authoritative source
2. `~/.clawmetry-gateway.json` should be treated as a cache/fallback, not the primary source
3. On every startup, refresh the token from the live config
4. Best fix: in `_load_gw_config()`, call `_detect_gateway_token()` FIRST (which reads the real config), THEN fall back to `~/.clawmetry-gateway.json`

The fix should be in the dev dashboard at: `/home/vivek/clawmetry-dev/dashboard.py`

### How to test
```bash
# On this machine (Dhriti):
cd ~/clawmetry-dev
python3 dashboard.py &
# In another terminal:
REAL_TOKEN=$(python3 -c "import json; print(json.load(open('/home/vivek/.openclaw/openclaw.json'))['gateway']['auth']['token'])")
curl "http://localhost:8095/api/auth/check?token=$REAL_TOKEN"
# Should return {"authRequired": true, "valid": true}
```

## Bug 2: Reconcile OSS sync daemon

The OSS source at `/home/vivek/.openclaw/workspace/clawmetry/packages/clawmetry/sync_patched.py` (1083 lines) is missing several critical fixes that exist in the installed version at `/home/vivek/.clawmetry/lib/python3.9/site-packages/clawmetry/sync.py` on the Mac (192.168.178.56).

### Fixes to port into sync_patched.py:

1. **Deleted files glob**: `sync_sessions` should also sync `*.jsonl.deleted.*` files (completed sub-agent sessions)
   ```python
   jsonl_files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")) + glob.glob(os.path.join(sessions_dir, "*.jsonl.deleted.*")))
   ```

2. **session_id in _flush_session_batch**: Extract UUID from filename and send it separately
   ```python
   session_id = fname.split(".")[0]  # "uuid.jsonl" or "uuid.jsonl.deleted.ts" -> "uuid"
   # Include session_id in the POST alongside encrypted blob
   ```

3. **Cron path resolution**: Check multiple paths
   ```python
   candidates = [
       os.path.join(home, ".openclaw", "cron", "jobs.json"),
       os.path.join(home, ".openclaw", "agents", "main", "cron", "jobs.json"),
   ]
   # Handle both {"jobs": [...]} and [...] formats
   ```

4. **sync_logs hang prevention**: Cap lines per cycle to prevent blocking on large (100MB+) log files

5. **PID file management** (sync_patched.py already has this from earlier fix - verify it's there)

You can SSH to Mac-Diya to read the installed sync.py:
```bash
ssh vivek@192.168.178.56 "cat ~/.clawmetry/lib/python3.9/site-packages/clawmetry/sync.py"
```

### Important
- Do NOT break backward compatibility
- Do NOT change encryption logic
- Test the glob change handles both active (.jsonl) and deleted (.jsonl.deleted.*) files
- The session_id must be the UUID portion only (no .jsonl suffix)
