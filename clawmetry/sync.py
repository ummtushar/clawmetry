"""
clawmetry/sync.py — Cloud sync daemon for clawmetry connect.

Reads local OpenClaw sessions/logs, encrypts with AES-256-GCM (E2E),
and streams to ingest.clawmetry.com. The encryption key never leaves
the local machine — cloud stores ciphertext only.
"""
from __future__ import annotations
import json
import os
import sys
import time
import glob
import base64
import secrets
import logging
import platform
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

INGEST_URL = os.environ.get("CLAWMETRY_INGEST_URL", "https://ingest.clawmetry.com")
CONFIG_DIR  = Path.home() / ".clawmetry"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE  = CONFIG_DIR / "sync-state.json"
LOG_FILE    = CONFIG_DIR / "sync.log"

POLL_INTERVAL = 15    # seconds between sync cycles
STREAM_INTERVAL = 2   # seconds between real-time stream pushes
BATCH_SIZE    = 10    # events per encrypted POST

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [clawmetry-sync] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
log = logging.getLogger("clawmetry.sync")


# ── Encryption (AES-256-GCM) ─────────────────────────────────────────────────

def generate_encryption_key() -> str:
    """Generate a new 256-bit key. Returns base64url string."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _get_aesgcm(key_b64: str):
    """Return an AESGCM cipher from a base64url key."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.urlsafe_b64decode(key_b64 + "==")
        return AESGCM(raw)
    except ImportError:
        raise RuntimeError(
            "E2E encryption requires the 'cryptography' package.\n"
            "  pip install cryptography"
        )


def encrypt_payload(data: dict, key_b64: str) -> str:
    """
    Encrypt a dict as AES-256-GCM.
    Returns base64url(nonce || ciphertext) — a single opaque string.
    Cloud stores this blob and never sees plaintext.
    """
    cipher = _get_aesgcm(key_b64)
    nonce  = secrets.token_bytes(12)          # 96-bit nonce (GCM standard)
    plain  = json.dumps(data).encode()
    ct     = cipher.encrypt(nonce, plain, None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_payload(blob: str, key_b64: str) -> dict:
    """Decrypt a blob produced by encrypt_payload. Used by clients."""
    cipher = _get_aesgcm(key_b64)
    raw    = base64.urlsafe_b64decode(blob + "==")
    nonce, ct = raw[:12], raw[12:]
    return json.loads(cipher.decrypt(nonce, ct, None))


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"No config at {CONFIG_FILE}. Run: clawmetry connect")
    return json.loads(CONFIG_FILE.read_text())


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    CONFIG_FILE.chmod(0o600)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_event_ids": {}, "last_log_offsets": {}, "last_sync": None}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post(path: str, payload: dict, api_key: str, timeout: int = 45) -> dict:
    url  = INGEST_URL.rstrip("/") + path
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-Api-Key": api_key}
    if payload.get("node_id"):
        headers["X-Node-Id"] = payload["node_id"]
    req  = urllib.request.Request(
        url, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}: {e.read().decode()[:200]}")


def validate_key(api_key: str) -> dict:
    return _post("/auth", {"api_key": api_key}, api_key)


# ── Path detection ─────────────────────────────────────────────────────────────


def _detect_docker_openclaw() -> dict:
    """Auto-detect OpenClaw running in Docker and find its data paths on the host."""
    import subprocess, json as _json
    result = {}
    try:
        # Find containers with openclaw/clawd in name or image
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}}	{{.Names}}	{{.Image}}	{{.Mounts}}"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return {}
        for line in out.stdout.strip().splitlines():
            parts = line.split("	")
            if len(parts) < 3:
                continue
            cid, name, image = parts[0], parts[1], parts[2]
            if not any(k in (name + image).lower() for k in ["openclaw", "clawd", "claw"]):
                continue
            log.info(f"Found OpenClaw Docker container: {name} ({image}) id={cid}")
            # Get volume mounts via docker inspect
            try:
                insp = subprocess.run(
                    ["docker", "inspect", "--format", "{{json .Mounts}}", cid],
                    capture_output=True, text=True, timeout=5)
                mounts = _json.loads(insp.stdout.strip()) if insp.returncode == 0 else []
                for m in mounts:
                    src = m.get("Source", "")
                    dst = m.get("Destination", "")
                    # Look for data/workspace/sessions mounts
                    if "agents" in dst or "sessions" in dst or "/data" == dst or "openclaw" in dst.lower():
                        log.info(f"  Mount: {src} -> {dst}")
                        if "sessions" in dst:
                            result["sessions_dir"] = src
                        elif "agents" in dst:
                            result["sessions_dir"] = os.path.join(src, "main", "sessions")
                        elif dst == "/data":
                            s = os.path.join(src, "agents", "main", "sessions")
                            if os.path.isdir(s):
                                result["sessions_dir"] = s
                            w = os.path.join(src, "workspace")
                            if os.path.isdir(w):
                                result["workspace"] = w
                    if "workspace" in dst:
                        result["workspace"] = src
                    if "logs" in dst or "tmp" in dst:
                        result["log_dir"] = src
            except Exception as e:
                log.debug(f"Docker inspect error: {e}")
            # If no volume mounts found, try docker exec to find paths
            if not result:
                try:
                    for check_path in ["/root/.openclaw", "/data", "/app"]:
                        chk = subprocess.run(
                            ["docker", "exec", cid, "ls", f"{check_path}/agents/main/sessions"],
                            capture_output=True, text=True, timeout=5)
                        if chk.returncode == 0 and chk.stdout.strip():
                            log.info(f"  Found sessions inside container at {check_path}")
                            # Copy files out to host
                            host_dir = Path.home() / ".clawmetry" / "docker-mirror"
                            host_dir.mkdir(parents=True, exist_ok=True)
                            sessions_mirror = host_dir / "sessions"
                            workspace_mirror = host_dir / "workspace"
                            sessions_mirror.mkdir(exist_ok=True)
                            workspace_mirror.mkdir(exist_ok=True)
                            # rsync from container
                            subprocess.run(["docker", "cp", f"{cid}:{check_path}/agents/main/sessions/.", str(sessions_mirror)],
                                           capture_output=True, timeout=30)
                            subprocess.run(["docker", "cp", f"{cid}:{check_path}/workspace/.", str(workspace_mirror)],
                                           capture_output=True, timeout=30)
                            # Copy logs
                            for log_path in ["/tmp/openclaw", f"{check_path}/logs"]:
                                subprocess.run(["docker", "cp", f"{cid}:{log_path}/.", str(host_dir / "logs")],
                                               capture_output=True, timeout=15)
                            result["sessions_dir"] = str(sessions_mirror)
                            result["workspace"] = str(workspace_mirror)
                            result["log_dir"] = str(host_dir / "logs")
                            result["docker_container"] = cid
                            result["docker_path"] = check_path
                            log.info(f"  Mirrored Docker data to {host_dir}")
                            break
                except Exception as e:
                    log.debug(f"Docker exec fallback error: {e}")
            if result:
                return result
    except FileNotFoundError:
        log.debug("Docker not installed or not in PATH")
    except Exception as e:
        log.debug(f"Docker detection error: {e}")
    return {}


def detect_paths() -> dict:
    home = Path.home()
    # Try Docker detection first (OpenClaw running in container)
    docker_paths = _detect_docker_openclaw()
    if docker_paths.get("sessions_dir"):
        log.info(f"Using Docker-detected paths: {docker_paths}")

    sessions_candidates = [
        home / ".openclaw" / "agents" / "main" / "sessions",
        Path("/data/agents/main/sessions"),
        Path("/app/agents/main/sessions"),
        Path("/root/.openclaw/agents/main/sessions"),
        Path("/opt/openclaw/agents/main/sessions"),
    ]
    oc_home = os.environ.get("OPENCLAW_HOME", "")
    if oc_home:
        sessions_candidates.insert(0, Path(oc_home) / "agents" / "main" / "sessions")
    sessions_dir = docker_paths.get("sessions_dir") or next((str(p) for p in sessions_candidates if p.exists()),
                        str(sessions_candidates[0]))

    log_candidates = [Path("/tmp/openclaw"), home / ".openclaw" / "logs", Path("/data/logs")]
    log_dir = docker_paths.get("log_dir") or next((str(p) for p in log_candidates if p.exists()), "/tmp/openclaw")

    workspace_candidates = [
        home / ".openclaw" / "workspace",
        Path("/data/workspace"),
        Path("/app/workspace"),
    ]
    workspace = docker_paths.get("workspace") or next((str(p) for p in workspace_candidates if p.exists()),
                     str(workspace_candidates[0]))

    log.info(f"Paths: sessions={sessions_dir} logs={log_dir} workspace={workspace}")
    return {"sessions_dir": sessions_dir, "log_dir": log_dir, "workspace": workspace}


# ── Sync: session events (full content, encrypted) ────────────────────────────

def sync_sessions(config: dict, state: dict, paths: dict) -> int:
    sessions_dir = paths["sessions_dir"]
    api_key      = config["api_key"]
    enc_key      = config.get("encryption_key")
    node_id      = config["node_id"]
    last_ids: dict = state.setdefault("last_event_ids", {})
    total = 0

    jsonl_files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")))
    for fpath in jsonl_files:
        fname    = os.path.basename(fpath)
        last_line = last_ids.get(fname, 0)
        batch: list[dict] = []

        try:
            with open(fpath, "r", errors="replace") as f:
                all_lines = f.readlines()

            new_lines = all_lines[last_line:]
            for i, raw in enumerate(new_lines, start=last_line):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue

                # Full content — encrypted before leaving machine
                batch.append(obj)

                if len(batch) >= BATCH_SIZE:
                    _flush_session_batch(batch, fname, api_key, enc_key, node_id)
                    total += len(batch)
                    batch = []

            if batch:
                _flush_session_batch(batch, fname, api_key, enc_key, node_id)
                total += len(batch)

            last_ids[fname] = len(all_lines)

        except Exception as e:
            log.warning(f"Session sync error ({fname}): {e}")

    return total


def _flush_session_batch(batch: list, fname: str, api_key: str,
                          enc_key: str | None, node_id: str) -> None:
    payload = {"session_file": fname, "node_id": node_id, "events": batch}
    if enc_key:
        _post("/ingest/events", {
            "node_id": node_id,
            "encrypted": True,
            "blob": encrypt_payload(payload, enc_key),
        }, api_key)
    else:
        _post("/ingest/events", payload, api_key)


# ── Sync: logs (full lines, encrypted) ────────────────────────────────────────

def sync_logs(config: dict, state: dict, paths: dict) -> int:
    log_dir  = paths["log_dir"]
    api_key  = config["api_key"]
    enc_key  = config.get("encryption_key")
    node_id  = config["node_id"]
    offsets: dict = state.setdefault("last_log_offsets", {})
    total = 0

    log_files = sorted(glob.glob(os.path.join(log_dir, "openclaw-*.log")))[-5:]
    for fpath in log_files:
        fname  = os.path.basename(fpath)
        offset = offsets.get(fname, 0)
        entries: list[dict] = []

        try:
            with open(fpath, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                if offset > size:
                    offset = 0
                f.seek(offset)
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entries.append(json.loads(raw))
                    except Exception:
                        entries.append({"raw": raw})
                    if len(entries) >= BATCH_SIZE:
                        _flush_log_batch(entries, fname, api_key, enc_key, node_id)
                        total += len(entries)
                        entries = []
                offsets[fname] = f.tell()

            if entries:
                _flush_log_batch(entries, fname, api_key, enc_key, node_id)
                total += len(entries)

        except Exception as e:
            log.warning(f"Log sync error ({fname}): {e}")

    return total


def _flush_log_batch(entries: list, fname: str, api_key: str,
                      enc_key: str | None, node_id: str) -> None:
    payload = {"log_file": fname, "node_id": node_id, "lines": entries}
    if enc_key:
        _post("/ingest/logs", {
            "node_id": node_id,
            "encrypted": True,
            "blob": encrypt_payload(payload, enc_key),
        }, api_key)
    else:
        _post("/ingest/logs", payload, api_key)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def send_heartbeat(config: dict) -> None:
    try:
        _post("/ingest/heartbeat", {
            "node_id": config["node_id"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "platform": platform.system(),
            "version": _get_version(),
            "e2e": bool(config.get("encryption_key")),
        }, config["api_key"])
    except Exception as e:
        log.debug(f"Heartbeat failed: {e}")


def _get_version() -> str:
    try:
        import re
        src = (Path(__file__).parent.parent / "dashboard.py").read_text(errors="replace")
        m = re.search(r'^__version__\s*=\s*["\'](.+?)["\']', src, re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


# ── Daemon loop ────────────────────────────────────────────────────────────────

def sync_crons(config: dict, state: dict, paths: dict) -> int:
    """Sync cron job definitions to cloud."""
    api_key = config["api_key"]
    node_id = config["node_id"]
    last_hash = state.get("cron_hash", "")

    # Find cron jobs.json
    home = Path.home()
    cron_candidates = [
        home / ".openclaw" / "cron" / "jobs.json",
        home / ".openclaw" / "agents" / "main" / "cron" / "jobs.json",
    ]
    cron_file = next((str(p) for p in cron_candidates if p.exists()), None)
    if not cron_file:
        return 0

    try:
        import hashlib
        raw = open(cron_file, "rb").read()
        h = hashlib.md5(raw).hexdigest()
        if h == last_hash:
            return 0
        data = json.loads(raw)
        jobs = data.get("jobs", []) if isinstance(data, dict) else data

        events = []
        for j in jobs:
            sched = j.get("schedule", {})
            kind = sched.get("kind", "")
            expr = sched.get("interval", "") if kind == "interval" else (
                   f"at {sched.get('at', '')}" if kind == "at" else
                   sched.get("cron", "") if kind == "cron" else "")
            events.append({
                "type": "cron_state", "session_id": "",
                "data": {"job_id": j.get("id",""), "name": j.get("name",""),
                         "enabled": j.get("enabled", True), "expr": expr}
            })

        if events:
            _post("/api/ingest", {"events": events, "node_id": node_id}, api_key)
            state["cron_hash"] = h
            return len(events)
    except Exception as e:
        log.warning(f"Cron sync error: {e}")
    return 0


def sync_session_metadata(config: dict) -> int:
    """Sync OpenClaw session metadata rows to cloud sessions table.
    
    Reads JSONL session files directly (HTTP API returns HTML, not JSON).
    Extracts session_id, model, timestamps from the event stream.
    """
    api_key = config["api_key"]
    node_id = config["node_id"]
    try:
        home = Path.home()
        sessions_candidates = [
            home / ".openclaw" / "agents" / "main" / "sessions",
            Path("/data/agents/main/sessions"),
        ]
        sessions_dir = next((p for p in sessions_candidates if p.exists()), None)
        if not sessions_dir:
            return 0

        session_rows = []
        for fpath in sorted(sessions_dir.glob("*.jsonl"))[-100:]:
            try:
                sid = fpath.stem  # UUID filename = session_id
                model = ""
                started_at = ""
                updated_at = ""
                total_tokens = 0
                total_cost = 0.0
                label = ""

                # Scan session file for metadata, tokens, cost, model
                # Read head for start info, scan all for usage, tail for end
                with open(fpath, "r", errors="replace") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        ts = ev.get("timestamp", "")
                        if not started_at and ts:
                            started_at = ts
                        if ts:
                            updated_at = ts
                        etype = ev.get("type", "")
                        if etype == "model_change" and ev.get("modelId"):
                            model = ev["modelId"]
                        elif etype == "session" and ev.get("label"):
                            label = ev["label"]
                        elif etype == "message":
                            msg = ev.get("message", {})
                            usage = msg.get("usage", {})
                            if usage:
                                total_tokens += int(usage.get("totalTokens", 0))
                                cost_obj = usage.get("cost", {})
                                if isinstance(cost_obj, dict):
                                    total_cost += float(cost_obj.get("total", 0))
                                elif isinstance(cost_obj, (int, float)):
                                    total_cost += float(cost_obj)
                            # Use last model seen in messages
                            msg_model = msg.get("model", "")
                            if msg_model:
                                model = msg_model

                session_rows.append({
                    "session_id": sid,
                    "display_name": label or sid[:8],
                    "status": "completed",
                    "model": model,
                    "total_tokens": total_tokens,
                    "total_cost": total_cost,
                    "started_at": started_at,
                    "updated_at": updated_at,
                })
            except Exception as e:
                log.debug(f"Session parse error ({fpath.name}): {e}")

        if not session_rows:
            return 0

        # Batch in groups of 50
        for i in range(0, len(session_rows), 50):
            batch = session_rows[i:i+50]
            _post("/ingest/sessions", {"node_id": node_id, "sessions": batch}, api_key)
        return len(session_rows)
    except Exception as e:
        log.warning(f"Session metadata sync failed: {e}")
        return 0


def sync_memory(config: dict, state: dict, paths: dict) -> int:
    """Sync memory files (MEMORY.md + memory/*.md) to cloud."""
    workspace = paths.get("workspace", "")
    api_key   = config["api_key"]
    enc_key   = config.get("encryption_key")
    node_id   = config["node_id"]
    last_hashes: dict = state.setdefault("memory_hashes", {})
    synced = 0

    # Collect all workspace memory files (same list as OSS dashboard)
    memory_files = []
    for name in ['MEMORY.md', 'SOUL.md', 'IDENTITY.md', 'USER.md', 'AGENTS.md', 'TOOLS.md', 'HEARTBEAT.md']:
        fpath = os.path.join(workspace, name)
        if os.path.isfile(fpath):
            memory_files.append((name, fpath))
    mem_dir = os.path.join(workspace, "memory")
    if os.path.isdir(mem_dir):
        for f in sorted(os.listdir(mem_dir)):
            if f.endswith(".md"):
                memory_files.append((f"memory/{f}", os.path.join(mem_dir, f)))

    if not memory_files:
        return 0

    # Check for changes via content hash
    import hashlib
    changed_files = []
    file_list = []
    for name, path in memory_files:
        try:
            content_bytes = open(path, "rb").read()
            h = hashlib.md5(content_bytes).hexdigest()
            file_list.append({"name": name, "size": len(content_bytes), "modified": os.path.getmtime(path)})
            if h != last_hashes.get(name):
                changed_files.append((name, content_bytes.decode("utf-8", errors="replace")))
                last_hashes[name] = h
        except Exception as e:
            log.debug(f"Memory file read error ({name}): {e}")

    if not changed_files:
        return 0

    # Push memory files as encrypted blob (like session events)
    payload = {
        "node_id": node_id,
        "memory_state": {"files": file_list},
        "memory_content": [{"path": name, "content": content[:100000]} for name, content in changed_files],
    }
    try:
        if enc_key:
            from clawmetry.sync import encrypt_payload
            _post("/ingest/memory", {
                "node_id": node_id,
                "encrypted": True,
                "blob": encrypt_payload(payload, enc_key),
            }, api_key)
        else:
            _post("/ingest/memory", payload, api_key)
        synced = len(changed_files)
    except Exception as e:
        log.warning(f"Memory sync error: {e}")

    return synced



# ── Real-time log streaming ────────────────────────────────────────────────────

def start_log_streamer(config: dict, paths: dict) -> threading.Thread:
    """Start a background thread that tails the local log file and POSTs lines to cloud in real-time."""
    api_key = config["api_key"]
    node_id = config["node_id"]
    log_dir = paths.get("log_dir", "")

    def _find_latest_log():
        if not log_dir or not os.path.isdir(log_dir):
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = sorted(glob.glob(os.path.join(log_dir, f"*{today}*")), reverse=True)
        if candidates:
            return candidates[0]
        # Fallback: most recent log file
        all_logs = sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)
        return all_logs[0] if all_logs else None

    def _stream_worker():
        log.info(f"Log streamer started — watching {log_dir}")
        current_file = None
        proc = None
        batch = []
        last_push = time.time()

        while True:
            try:
                # Find/rotate to latest log file
                latest = _find_latest_log()
                if latest != current_file:
                    if proc:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    current_file = latest
                    if not current_file:
                        time.sleep(5)
                        continue
                    proc = subprocess.Popen(
                        ["tail", "-f", "-n", "0", current_file],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    log.info(f"Tailing {current_file}")

                if not proc or not proc.stdout:
                    time.sleep(2)
                    continue

                # Non-blocking read with select
                import select
                ready, _, _ = select.select([proc.stdout], [], [], STREAM_INTERVAL)
                if ready:
                    line = proc.stdout.readline()
                    if line:
                        batch.append(line.rstrip())

                # Push batch every STREAM_INTERVAL seconds
                now = time.time()
                if batch and (now - last_push >= STREAM_INTERVAL or len(batch) >= 50):
                    try:
                        _post("/ingest/stream", {"node_id": node_id, "lines": batch}, api_key)
                    except Exception as e:
                        log.debug(f"Stream push error: {e}")
                    batch = []
                    last_push = now

            except Exception as e:
                log.debug(f"Stream worker error: {e}")
                time.sleep(5)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc = None
                    current_file = None

    t = threading.Thread(target=_stream_worker, daemon=True, name="log-streamer")
    t.start()
    return t


def run_daemon() -> None:
    config = load_config()
    # If node_id looks like email prefix (contains + or @), use hostname instead
    nid = config.get("node_id", "")
    if not nid or "+" in nid or "@" in nid:
        import socket
        config["node_id"] = socket.gethostname() or platform.node() or "unknown"
        save_config(config)
        log.info(f"Fixed node_id: {nid!r} → {config['node_id']!r}")
    paths  = detect_paths()
    enc    = "🔒 E2E encrypted" if config.get("encryption_key") else "⚠️  unencrypted"
    log.info(f"Starting sync daemon — node={config['node_id']} → {INGEST_URL} ({enc})")

    # ── First-run: full synchronous sync so customer sees data immediately ──
    send_heartbeat(config)
    log.info("Initial heartbeat sent")

    first_run = not STATE_FILE.exists()
    if first_run:
        log.info("First run detected — performing full initial sync...")
        state = load_state()
        try:
            mem = sync_memory(config, state, paths)
            log.info(f"  Memory: {mem} files synced")
        except Exception as e:
            log.warning(f"  Memory sync error: {e}")
        try:
            ev = sync_sessions(config, state, paths)
            log.info(f"  Sessions: {ev} events synced")
        except Exception as e:
            log.warning(f"  Session sync error: {e}")
        try:
            sm = sync_session_metadata(config)
            log.info(f"  Session metadata: {sm} rows synced")
        except Exception as e:
            log.warning(f"  Session metadata error: {e}")
        try:
            lg = sync_logs(config, state, paths)
            log.info(f"  Logs: {lg} lines synced")
        except Exception as e:
            log.warning(f"  Log sync error: {e}")
        try:
            cr = sync_crons(config, state, paths)
            log.info(f"  Crons: {cr} synced")
        except Exception as e:
            log.warning(f"  Cron sync error: {e}")
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        send_heartbeat(config)
        log.info("Initial sync complete — node fully visible in cloud")

    # Start real-time log streamer in background
    start_log_streamer(config, paths)

    heartbeat_interval = 60
    last_heartbeat = time.time()

    while True:
        try:
            state = load_state()
            ev = sync_sessions(config, state, paths)
            lg = sync_logs(config, state, paths)
            mem = sync_memory(config, state, paths)
            crons = sync_crons(config, state, paths)
            sm = sync_session_metadata(config)
            state["last_sync"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            if ev or lg or mem or crons or sm:
                log.info(f"Synced {ev} events, {lg} log lines, {mem} memory files, {crons} crons, {sm} session rows ({enc})")

            # Re-mirror Docker data if running in Docker mode
            if hasattr(detect_paths, "_docker_cid") or any("docker-mirror" in str(v) for v in paths.values()):
                try:
                    fresh = _detect_docker_openclaw()
                    if fresh.get("sessions_dir"):
                        paths.update({k: v for k, v in fresh.items() if k in paths})
                except Exception:
                    pass

            now = time.time()
            if now - last_heartbeat > heartbeat_interval:
                send_heartbeat(config)
                last_heartbeat = now

        except Exception as e:
            log.error(f"Sync cycle error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    while True:
        try:
            run_daemon()
            break  # clean exit
        except KeyboardInterrupt:
            break
        except Exception as e:
            import traceback
            log.error(f"Daemon crashed: {e}")
            log.error(traceback.format_exc())
            log.info("Restarting in 15 seconds...")
            time.sleep(15)
