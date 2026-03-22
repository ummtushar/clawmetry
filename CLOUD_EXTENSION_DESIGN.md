# ClawMetry Cloud Extension Design

> OSS stays clean. Cloud plugs in. Neither knows about the other.

---

## 1. Feature Matrix: OSS vs Cloud

| Feature | OSS (MIT) | Cloud (app.clawmetry.com) | Notes |
|---------|-----------|--------------------------|-------|
| **Local session monitoring** | ✅ | ✅ (via sync) | Core value — always OSS |
| **Token/cost analytics** | ✅ | ✅ | OSS reads local files |
| **Real-time dashboard** | ✅ | ✅ | Flask UI — OSS |
| **Cron management** | ✅ | ✅ | Via gateway WebSocket |
| **OTLP receiver** | ✅ | ✅ | OSS optional feature |
| **History (SQLite)** | ✅ | — | Local history.py |
| **Fleet/multi-node view** | ✅ (self-managed) | ✅ (managed) | OSS: self-host; Cloud: hosted |
| **Budget alerts (local)** | ✅ | ✅ | |
| **Cross-node correlation** | ❌ | ✅ | Requires central DB |
| **Multi-user / teams** | ❌ | ✅ | Auth, RBAC |
| **Persistent cloud history** | ❌ | ✅ | Turso + Firestore |
| **cm_ API key auth** | ❌ | ✅ | Cloud identity |
| **Billing / subscriptions** | ❌ | ✅ | Stripe etc. |
| **Cross-agent correlation** | ❌ | ✅ | e.g., Diya + Aisha + Dhriti in one view |
| **SSO / OAuth login** | ❌ | ✅ | |
| **Anomaly detection (ML)** | ❌ | ✅ | Needs historical corpus |
| **Team audit log** | ❌ | ✅ | |
| **Cloud Run hosted agent** | ❌ | ✅ | |
| **Mobile push alerts** | ❌ | ✅ | Firebase |
| **Webhook delivery** | ❌ | ✅ | |
| **Retention > local disk** | ❌ | ✅ | Configurable retention |

---

## 2. Extension System Design

### Core principle: OSS exposes hooks; Cloud registers handlers

The OSS codebase defines a **registry** of named extension points. It calls into registered handlers at the right moments. Cloud installs its extensions by importing and registering — OSS never imports Cloud code.

### 2.1 The `clawmetry.extensions` module

Add this to the OSS repo as `extensions.py` (one file, ~100 lines):

```python
# clawmetry/extensions.py  — OSS side
"""
Extension point registry for ClawMetry.
Cloud plugins register handlers here without modifying OSS code.
"""
from __future__ import annotations
import logging
from typing import Callable, Any

_log = logging.getLogger("clawmetry.extensions")

_registry: dict[str, list[Callable]] = {}


def register(event: str, handler: Callable) -> None:
    """Register a handler for a named extension point."""
    _registry.setdefault(event, []).append(handler)
    _log.debug("Extension registered: %s -> %s", event, handler.__qualname__)


def emit(event: str, payload: Any = None) -> list[Any]:
    """
    Emit an event to all registered handlers.
    Returns list of results (non-None). Handlers must not raise — errors are logged.
    """
    results = []
    for handler in _registry.get(event, []):
        try:
            result = handler(payload)
            if result is not None:
                results.append(result)
        except Exception as e:
            _log.error("Extension handler error [%s]: %s", event, e, exc_info=True)
    return results


def emit_first(event: str, payload: Any = None, default: Any = None) -> Any:
    """Emit and return first non-None result, or default."""
    results = emit(event, payload)
    return results[0] if results else default


def has_handlers(event: str) -> bool:
    return bool(_registry.get(event))
```

### 2.2 Extension Points in OSS (what to add to dashboard.py)

These are the named hooks OSS exposes. **OSS emits; Cloud handles.**

```python
# At startup — extensions can inject middleware, extra routes, etc.
extensions.emit("startup", {"app": app, "config": config})

# When a session snapshot is built (for /api/overview)
extensions.emit("session.snapshot", {"session_id": sid, "data": snapshot})

# When usage data is compiled (for /api/usage)
extensions.emit("usage.compiled", {"totals": totals, "by_model": by_model})

# When a fleet node registers
extensions.emit("fleet.node_register", {"node_id": nid, "meta": meta})

# When a fleet metric heartbeat arrives
extensions.emit("fleet.metric", {"node_id": nid, "metrics": metrics})

# When a budget alert fires
extensions.emit("budget.alert", {"level": "warning", "spend": x, "limit": y})

# Auth: OSS asks extensions if a request is authenticated
# Returns None (no opinion) or {"user": ..., "tier": ...}
user = extensions.emit_first("auth.check", {"token": token, "request": request})

# Extra API routes Cloud wants to add
extensions.emit("routes.register", {"app": app})

# Shutdown / cleanup
extensions.emit("shutdown", {})
```

### 2.3 How OSS loads Cloud extensions

OSS does **not** import Cloud code. Instead, OSS checks for a well-known entry point at startup:

```python
# In dashboard.py __main__ startup block:
def _load_extensions():
    """Auto-discover installed extension packages."""
    import importlib
    import pkg_resources
    for ep in pkg_resources.iter_entry_points("clawmetry.extensions"):
        try:
            ep.load()  # The extension registers itself on import
            print(f"[clawmetry] Extension loaded: {ep.name}")
        except Exception as e:
            print(f"[clawmetry] Extension load failed ({ep.name}): {e}")

_load_extensions()
```

Cloud package declares in its `setup.py` / `pyproject.toml`:
```toml
[project.entry-points."clawmetry.extensions"]
cloud = "clawmetry_cloud.extension:register_all"
```

When Cloud is installed (`pip install clawmetry-cloud`), it auto-registers. No OSS changes needed ever again.

---

## 3. Cloud Extension Package Structure

Cloud lives in a **separate private repo** (`clawmetry-cloud`). It depends on OSS `clawmetry` as a library:

```
clawmetry-cloud/
├── pyproject.toml              # entry_points: clawmetry.extensions = cloud:register_all
├── clawmetry_cloud/
│   ├── __init__.py
│   ├── extension.py            # register_all() — called on import
│   ├── auth/
│   │   ├── __init__.py
│   │   └── cm_keys.py          # cm_ key validation, tier lookup
│   ├── sync/
│   │   ├── __init__.py
│   │   └── turso_writer.py     # DataProvider → Turso
│   ├── fleet/
│   │   ├── __init__.py
│   │   └── multi_node.py       # Cross-node correlation
│   ├── billing/
│   │   ├── __init__.py
│   │   └── stripe_sync.py
│   ├── routes/
│   │   ├── __init__.py
│   │   └── cloud_routes.py     # /api/cloud/* endpoints
│   └── push/
│       ├── __init__.py
│       └── firebase.py         # Mobile push alerts
```

`extension.py`:
```python
# clawmetry_cloud/extension.py
from clawmetry import extensions

def register_all():
    from .auth.cm_keys import check_auth
    from .sync.turso_writer import on_session_snapshot, on_usage_compiled, on_fleet_metric
    from .fleet.multi_node import on_node_register
    from .routes.cloud_routes import register_routes
    from .push.firebase import on_budget_alert

    extensions.register("auth.check", check_auth)
    extensions.register("session.snapshot", on_session_snapshot)
    extensions.register("usage.compiled", on_usage_compiled)
    extensions.register("fleet.metric", on_fleet_metric)
    extensions.register("fleet.node_register", on_node_register)
    extensions.register("budget.alert", on_budget_alert)
    extensions.register("routes.register", register_routes)
```

---

## 4. Sync Daemon Architecture

The current `clawmetry-cloud-sync.py` is a standalone script. In the new architecture:

### Option A: Sync daemon pushes to a DataProvider (recommended)

```
clawmetry-cloud-sync (daemon)
    ↓ reads ~/.openclaw files (same as OSS dashboard)
    ↓ pushes structured events to:
        → ingest.clawmetry.com/api/ingest  (HTTP POST, cm_ auth)
            → Cloud Run ingest service
                → Turso DB (sessions, metrics, costs)
                → Firestore (real-time fleet status)
```

The daemon stays **separate from the dashboard process** — it's a sidecar that runs alongside. This is the right call because:
- Dashboard (OSS) stays read-only and stateless
- Sync daemon can run independently, even when dashboard is off
- Ingest service can validate, deduplicate, and route data properly

### Option B: Extension writes directly to Turso (avoid)
Putting Turso credentials into an extension that loads inside the OSS process is messy and violates the privacy principle. Ingest via HTTP is cleaner.

### Sync Daemon Improvements for New Architecture

The daemon should use the extension's `on_session_snapshot` shape so Cloud backend and daemon agree on schema:

```python
# Standardized ingest payload (shared type, defined in clawmetry_cloud)
@dataclass
class IngestPayload:
    node_id: str
    cm_token: str
    event_type: str  # "session_snapshot" | "metric_batch" | "fleet_heartbeat"
    timestamp: str   # ISO8601
    data: dict
```

The sync daemon sends these. The ingest service on Cloud Run writes to Turso (sessions/metrics) and Firestore (live state).

---

## 5. Minimal OSS Changes Required (One-Time Refactor)

This is the complete list of changes to `dashboard.py` to enable the extension system. After this, Cloud can add features **without ever touching OSS again**:

1. **Add `extensions.py`** to the package (or inline in dashboard.py as a class)
2. **Add `_load_extensions()` call** at startup (5 lines)
3. **Add ~8 `extensions.emit()` calls** at the right spots:
   - After session snapshot is built
   - After usage is compiled
   - On fleet node register/heartbeat
   - On budget alert fire
   - In auth middleware (one `emit_first` call)
   - On startup/shutdown
   - In routes setup
4. **Make `FLEET_KEY` auth defer to extensions** (currently hardcoded env var)
5. **Extract history writing** to emit a hook (so Cloud can replace SQLite with Turso)

That's it. ~30 lines of changes to a 15k-line file. Perfectly surgical.

---

## 6. How Cloud Inherits from OSS (Without Forking)

**Don't fork. Depend.**

```
pip install clawmetry          # OSS — PyPI, MIT
pip install clawmetry-cloud    # Cloud — private PyPI / direct install
```

Cloud's `pyproject.toml`:
```toml
[project]
name = "clawmetry-cloud"
dependencies = [
    "clawmetry>=0.10.11",   # pins minimum OSS version
]

[project.entry-points."clawmetry.extensions"]
cloud = "clawmetry_cloud.extension:register_all"
```

The Cloud Run container runs:
```dockerfile
FROM python:3.12-slim
RUN pip install clawmetry clawmetry-cloud
CMD ["clawmetry", "--host", "0.0.0.0", "--port", "8080"]
```

When `clawmetry` starts, it discovers and loads `clawmetry_cloud` via entry points. The Cloud dashboard is OSS dashboard + Cloud extensions.

**OSS upgrades flow downstream automatically.** Cloud just pins a minimum version and tests compatibility. No merge conflicts. No divergence. OSS stays the source of truth.

---

## 7. Summary

| Concern | Solution |
|---------|----------|
| `if CLOUD_MODE:` spaghetti | Replaced by `extensions.emit()` — OSS never checks cloud state |
| Cloud code in OSS repo | Never — Cloud is a separate private package |
| OSS changes for Cloud features | Zero after initial refactor |
| Cloud inheriting OSS | `pip install clawmetry` as dependency, not a fork |
| Sync daemon integration | Stays standalone; pushes to ingest API via HTTP |
| Auth | Cloud extension handles `auth.check` hook |
| New Cloud routes | Cloud extension handles `routes.register` hook |
| Data storage | Sync daemon → ingest API → Turso/Firestore; OSS never touches cloud DB |
