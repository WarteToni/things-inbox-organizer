#!/usr/bin/env python3
"""bridge.py - Core logic for the Hermes -> Things bridge.

Data flow:
  LLM extracts {title, when, notes} from a user reminder request
    -> add_things_todo()
       1. normalize `when` (ISO date or Things keyword)
       2. build  things:///add?title=<enc>&when=<v>&notes=<enc>
       3. persist the todo to a durable queue file (status=open)
       4. POST to Bark with the Things URL in the `url` field
       5. on success mark the queue entry imported; on failure it stays
          open so a later flush retries it (nothing is silently lost)

Security: the Bark device key is read from THINGS_BARK_KEY (env var, or
~/.hermes/.env fallback). It is never hardcoded and never logged.

All queue I/O uses fcntl locking + atomic rename so concurrent Hermes
processes never observe a half-written file.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger("things_bridge")

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

DEFAULT_QUEUE_PATH = "/root/.hermes/todos/todos.json"
DEFAULT_BARK_HOST = "https://api.day.app"
DEFAULT_ENV_FILE = "/root/.hermes/.env"
LOG_PATH = os.environ.get(
    "THINGS_BRIDGE_LOG", "/root/.hermes/logs/things-bridge.log"
)

# Things `when` keywords accepted as-is (case-insensitive on input).
THINGS_WHEN_KEYWORDS = {"today", "tomorrow", "evening", "anytime", "someday"}

# ---------------------------------------------------------------------------
# Auto-classification: keyword -> Things Area/Project list name.
# Used as a FALLBACK when the caller does not pass an explicit `list`.
# The live copy is loaded from LIST_CLASSIFY_CONFIG_FILE (hot-reloaded on
# every call via mtime check); this constant is the built-in fallback and
# the source of truth the file was seeded from (user-confirmed 2026-08-12).
# Rule order matters: first match wins.
# ---------------------------------------------------------------------------
LIST_CLASSIFY_RULES: dict[str, list[str]] = {
    "Acme IVD": ["Acme", "acme", "ivd", "ngs", "体外诊断"],
    "Ginkgo Pharma": [
        "Ginkgo", "原料药", "api", "报价", "客户", "询价",
        "巴基斯坦", "俄罗斯", "海外客户",
    ],
    "Family": ["爸妈", "父母", "爸爸", "妈妈", "体检", "健康", "老家"],
    "Personal": ["学习", "课程", "读书", "健身", "理财", "生活", "成长"],
}

# External editable copy of the rules above; mtime-cached, hot-reloaded on
# every add_things_todo call — edit this file and the change takes effect on
# the NEXT tool call without any gateway restart.
LIST_CLASSIFY_CONFIG_FILE = "/root/.hermes/todos/list-classify-rules.json"

BARK_RETRIES = 3
BARK_TIMEOUT = 10  # seconds per attempt
BARK_BACKOFF_BASE = 1.0  # seconds; doubled per retry

_QUEUE_NOTE = (
    "_说明: Hermes -> Things 待办桥接队列。status: open=待推送, "
    "imported=已推送到 iPhone Bark。由 things-bridge 插件自动维护，勿手工乱改。"
)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CN_MONTH_DAY_RE = re.compile(r"^\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]?\s*$")
_CN_WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


def queue_path() -> str:
    return os.environ.get("TODO_PATH", DEFAULT_QUEUE_PATH)


# ---------------------------------------------------------------------------
# Hot-reload support (no gateway restart needed after edits)
# ---------------------------------------------------------------------------

def reload_bridge_module():
    """Re-execute this module from disk and return the fresh module object.

    Plugin handlers are registered once at gateway startup, binding the
    bridge module that existed at that moment. To let code edits take effect
    inside the already-running gateway, the tool handler in __init__.py calls
    this on EVERY invocation: the module is re-read from its file path
    (sys.modules is updated as a side effect) so the handler always executes
    the latest code on disk.

    Safety: if re-execution fails for ANY reason (syntax error, missing
    file...), the previously loaded module is returned unchanged, so a bad
    edit degrades gracefully to the last known-good code instead of breaking
    reminder creation. Never raises.
    """
    import importlib.util

    current = sys.modules.get(__name__)
    path = __file__
    try:
        spec = importlib.util.spec_from_file_location(__name__, path)
        if spec is None or spec.loader is None:
            return current
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)  # raises on syntax errors -> caught
        sys.modules[__name__] = fresh
        return fresh
    except Exception as exc:  # noqa: BLE001 - always fall back to old code
        try:
            logger.warning(
                "bridge hot-reload failed (%s: %s); using previous module",
                type(exc).__name__, exc,
            )
        except Exception:
            pass
        return current


# ---------------------------------------------------------------------------
# Classification rules: live config file with mtime-based hot reload
# ---------------------------------------------------------------------------

_rules_cache: tuple = (None, None)  # (mtime_ns, rules dict)


def _get_classify_rules() -> dict[str, list[str]]:
    """Return the active keyword rules: LIST_CLASSIFY_CONFIG_FILE if it
    exists and is valid, else the built-in LIST_CLASSIFY_RULES.

    The file is re-checked via mtime_ns on every call (negligible cost), so
    editing it takes effect on the next add_things_todo call without any
    restart. A corrupt file is ignored (falls back to built-in rules).
    """
    global _rules_cache
    try:
        st = os.stat(LIST_CLASSIFY_CONFIG_FILE)
    except OSError:
        _rules_cache = (None, None)
        return LIST_CLASSIFY_RULES
    cached_mtime, cached_rules = _rules_cache
    if cached_mtime == st.st_mtime_ns and cached_rules is not None:
        return cached_rules
    try:
        with open(LIST_CLASSIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = {
            str(k): [str(kw).lower() for kw in (v or [])]
            for k, v in data.items()
            if isinstance(v, (list, tuple))
        }
    except Exception:  # noqa: BLE001 - corrupt file -> built-in fallback
        _rules_cache = (None, None)
        return LIST_CLASSIFY_RULES
    _rules_cache = (st.st_mtime_ns, rules)
    return rules


def classify_todo_list(title: str, notes: str = "") -> str:
    """Match title/notes against the keyword rules.

    Returns the Things list (Area/Project) name on a hit, or "" when nothing
    matches (todo then lands in the Things inbox). Matching is
    case-insensitive substring search over title + notes; first matching
    rule wins.
    """
    text = f"{title or ''} {notes or ''}".lower()
    if not text.strip():
        return ""
    for list_name, keywords in _get_classify_rules().items():
        if any(kw and kw in text for kw in keywords):
            return list_name
    return ""


# ---------------------------------------------------------------------------
# .env fallback (plugin subprocess env may be scrubbed)
# ---------------------------------------------------------------------------

def _read_env_file(path: str = DEFAULT_ENV_FILE) -> dict:
    """Parse KEY=VALUE lines from a dotenv file. Never raises."""
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def get_config(env_file: str = DEFAULT_ENV_FILE) -> dict:
    """Return {bark_key, bark_host}. Env vars win, .env file is fallback."""
    file_vars = _read_env_file(env_file)
    key = os.environ.get("THINGS_BARK_KEY") or file_vars.get("THINGS_BARK_KEY", "")
    host = (
        os.environ.get("THINGS_BARK_HOST")
        or file_vars.get("THINGS_BARK_HOST", "")
        or DEFAULT_BARK_HOST
    ).rstrip("/")
    return {"bark_key": key.strip(), "bark_host": host}


def is_configured(env_file: str = DEFAULT_ENV_FILE) -> bool:
    return bool(get_config(env_file)["bark_key"])


# ---------------------------------------------------------------------------
# Date normalization (defense-in-depth; the LLM should already emit ISO)
# ---------------------------------------------------------------------------

def normalize_when(raw: str, now: _dt.date | None = None) -> str | None:
    """Normalize a `when` value to an ISO date or a Things keyword.

    Returns None when the value cannot be understood. Accepts:
      - ISO dates (2026-08-14) validated for real calendar dates
      - Things keywords: today/tomorrow/evening/anytime/someday
      - Chinese fallbacks: 今天/明天/后天/大后天/周X/下周X/X月X号/月底
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    today = now or _dt.datetime.now().date()

    low = s.lower()
    if low in THINGS_WHEN_KEYWORDS:
        return low
    m = _ISO_DATE_RE.match(s)
    if m:
        try:
            _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return s

    # ---- Chinese fallbacks ----
    if s in ("今天", "今日"):
        return "today"
    if s in ("明天", "明日"):
        return "tomorrow"
    if s == "后天":
        return (today + _dt.timedelta(days=2)).isoformat()
    if s == "大后天":
        return (today + _dt.timedelta(days=3)).isoformat()
    if s == "月底":
        if today.month == 12:
            last = _dt.date(today.year, 12, 31)
        else:
            last = _dt.date(today.year, today.month + 1, 1) - _dt.timedelta(days=1)
        return last.isoformat()

    # 周X / 星期X / 礼拜X, optionally prefixed with 下 (next week)
    m = re.match(r"^\s*(下)?\s*(?:周|星期|礼拜)([一二三四五六日天])\s*$", s)
    if m:
        next_week = bool(m.group(1))
        target_wd = _CN_WEEKDAY_MAP[m.group(2)]
        if next_week:
            # next ISO week's Monday, then offset by weekday index
            days_to_next_monday = (7 - today.weekday()) % 7 or 7
            next_monday = today + _dt.timedelta(days=days_to_next_monday)
            return (next_monday + _dt.timedelta(days=target_wd)).isoformat()
        delta = (target_wd - today.weekday()) % 7  # 0 means today
        return (today + _dt.timedelta(days=delta)).isoformat()

    # X月X号 / X月X日 (rolls to next year if long past)
    m = _CN_MONTH_DAY_RE.match(s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = _dt.date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today - _dt.timedelta(days=3):
            try:
                candidate = _dt.date(today.year + 1, month, day)
            except ValueError:
                return None
        return candidate.isoformat()

    return None


# ---------------------------------------------------------------------------
# Things URL construction
# ---------------------------------------------------------------------------

def build_things_url(title: str, when: str, notes: str = "", list_name: str = "") -> str:
    """Build a things:///add URL. Text fields are percent-encoded.

    ``list_name`` (optional) pins the todo to a Things Area/Project by name;
    omit it to let the todo land in the inbox.
    """
    params = [("title", title)]
    if when:
        params.append(("when", when))
    if notes:
        params.append(("notes", notes))
    if list_name:
        params.append(("list", list_name))
    query = "&".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params
    )
    return f"things:///add?{query}"


def _when_display(when: str) -> str:
    """Human-readable date fragment for the push body."""
    m = _ISO_DATE_RE.match(when or "")
    if m:
        return f"{int(m.group(2))}月{int(m.group(3))}日"
    return {"today": "今天", "tomorrow": "明天", "evening": "今晚"}.get(when, "")


# ---------------------------------------------------------------------------
# Durable queue (fcntl + atomic rename)
# ---------------------------------------------------------------------------

def _load_queue(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return {"_说明": _QUEUE_NOTE, "todos": []}
    except json.JSONDecodeError:
        logger.error("queue file %s is corrupt; refusing to overwrite", path)
        raise


def _save_queue_atomic(path: str, data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".todos_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _with_queue_lock(path: str, fn):
    """Run fn(data) under an exclusive lock and persist the result.

    fn mutates the loaded dict in place and returns a payload.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _load_queue(path)
        result = fn(data)
        _save_queue_atomic(path, data)
        return result
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def enqueue_todo(path: str, title: str, when: str, notes: str, list_name: str = "") -> str:
    """Append an open todo. Idempotent on (title, when) within open items.

    Returns the todo id. ``list_name`` (Things Area/Project) is stored on the
    entry so flush_queue() can rebuild the exact same things URL on retry.
    """
    def _do(data: dict) -> str:
        todos = data.setdefault("todos", [])
        for t in todos:
            if (
                isinstance(t, dict)
                and t.get("status") == "open"
                and t.get("title") == title
                and t.get("when") == when
            ):
                return str(t.get("id"))  # dedupe: same reminder already queued
        todo_id = uuid.uuid4().hex[:10]
        todos.append({
            "id": todo_id,
            "title": title,
            "notes": notes,
            "when": when,
            "list": list_name or "",
            "status": "open",
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "attempts": 0,
            "last_error": "",
        })
        return todo_id

    return _with_queue_lock(path, _do)


def _mark(path: str, todo_id: str, status: str, extra: dict | None = None) -> bool:
    def _do(data: dict) -> bool:
        for t in data.get("todos", []):
            if isinstance(t, dict) and str(t.get("id")) == todo_id:
                t["status"] = status
                t.update(extra or {})
                return True
        return False

    return _with_queue_lock(path, _do)


def mark_imported(path: str, todo_id: str, bark_response: str = "") -> bool:
    return _mark(path, todo_id, "imported", {
        "imported_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "bark_response": bark_response[:200],
    })


def mark_attempt_failed(path: str, todo_id: str, attempts: int, error: str) -> None:
    _mark(path, todo_id, "open", {"attempts": attempts, "last_error": error[:200]})


def list_open(path: str) -> list:
    data = _load_queue(path)
    return [t for t in data.get("todos", []) if t.get("status") == "open"]


# ---------------------------------------------------------------------------
# Bark push
# ---------------------------------------------------------------------------

def _bark_log(msg: str) -> None:
    """Append to a dedicated log file; never raises."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"{_dt.datetime.now().isoformat(timespec='seconds')} {msg}\n"
            )
    except OSError:
        pass


def push_bark(
    key: str,
    host: str,
    push_title: str,
    body: str,
    url: str,
    retries: int = BARK_RETRIES,
) -> dict:
    """POST a push to Bark. Returns {ok, response|error, attempts}."""
    endpoint = f"{host.rstrip('/')}/{key}"
    payload = json.dumps({
        "title": push_title,
        "body": body,
        "group": "Hermes-Things",
        "url": url,
        "level": "timeSensitive",
    }, ensure_ascii=False).encode("utf-8")

    last_error = "unknown"
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=BARK_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                if resp.status == 200 and parsed.get("code") == 200:
                    _bark_log(f"OK attempts={attempt} body={body!r}")
                    return {"ok": True, "response": parsed, "attempts": attempt}
                last_error = f"HTTP {resp.status}: {raw[:200]}"
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_error = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001 - never propagate to agent loop
            last_error = f"{type(e).__name__}: {e}"
        _bark_log(f"RETRY attempt={attempt}/{retries} error={last_error}")
        if attempt < retries:
            time.sleep(BARK_BACKOFF_BASE * (2 ** (attempt - 1)))
    _bark_log(f"FAIL body={body!r} error={last_error}")
    return {"ok": False, "error": last_error, "attempts": retries}


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def add_things_todo(
    title: str,
    when: str,
    notes: str = "",
    list_name: str = "",
    queue_file: str | None = None,
    env_file: str = DEFAULT_ENV_FILE,
) -> dict:
    """Create a Things todo via Bark push. Returns a JSON-serializable dict.

    Never raises - every failure mode is reported in the return payload so
    the LLM can tell the user instead of silently dropping the reminder.

    ``list_name`` pins the todo to a Things Area/Project. When empty, the
    built-in keyword dictionary (LIST_CLASSIFY_RULES / config file) tries to
    classify the todo by matching title+notes; a hit fills in the list, a
    miss leaves the todo in the Things inbox. An explicit ``list_name``
    always wins over the dictionary.
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "title 不能为空"}

    when_norm = normalize_when(when or "")
    if when_norm is None:
        return {
            "ok": False,
            "error": (
                f"无法解析日期 {when!r}。请输出 YYYY-MM-DD（如 2026-08-14）"
                "或 today/tomorrow/evening/anytime/someday"
            ),
        }

    cfg = get_config(env_file)
    if not cfg["bark_key"]:
        return {
            "ok": False,
            "error": "THINGS_BARK_KEY 未配置（应写入 /root/.hermes/.env）",
        }

    # Classification: explicit list wins; otherwise keyword dictionary; no
    # match -> "" (Things inbox).
    list_norm = (list_name or "").strip()
    classified = False
    if not list_norm:
        matched = classify_todo_list(title, notes)
        if matched:
            list_norm = matched
            classified = True

    path = queue_file or queue_path()
    things_url = build_things_url(title, when_norm, notes, list_norm)
    todo_id = enqueue_todo(path, title, when_norm, notes, list_norm)

    # Best-effort: flush any previously failed open items too (dedup inside).
    results = flush_queue(path, env_file=env_file, _skip_lock_ids={todo_id} if False else None)

    # flush_queue handled everything including our new item; find our result.
    mine = next((r for r in results if r.get("id") == todo_id), None)
    if mine and mine.get("ok"):
        msg = "已推送到 iPhone（Bark），点一下通知即在 Things 创建待办"
        if list_norm:
            src = "词典自动归类" if classified else "指定清单"
            msg += f"，归入「{list_norm}」（{src}）"
        return {
            "ok": True,
            "todo_id": todo_id,
            "title": title,
            "when": when_norm,
            "list": list_norm,
            "classified": classified,
            "things_url": things_url,
            "message": msg,
        }
    err = (mine or {}).get("error", "未知错误")
    return {
        "ok": False,
        "todo_id": todo_id,
        "error": f"Bark 推送失败（已入队列，后续自动重试）: {err}",
    }


def flush_queue(
    path: str | None = None,
    env_file: str = DEFAULT_ENV_FILE,
    _skip_lock_ids=None,
) -> list:
    """Try to push every open queue item. Returns per-item results."""
    path = path or queue_path()
    cfg = get_config(env_file)
    if not cfg["bark_key"]:
        return []
    results = []
    for item in list_open(path):
        todo_id = str(item.get("id"))
        title = str(item.get("title", ""))
        when = str(item.get("when", ""))
        notes = str(item.get("notes", ""))
        list_name = str(item.get("list", "") or "")  # stored at enqueue time
        url = build_things_url(title, when, notes, list_name)
        date_disp = _when_display(when)
        body = f"{title}（{date_disp}）" if date_disp else title
        res = push_bark(
            cfg["bark_key"], cfg["bark_host"],
            "📥 Hermes 新待办 → 点我进 Things", body, url,
        )
        if res["ok"]:
            mark_imported(path, todo_id, json.dumps(res.get("response", {}), ensure_ascii=False))
            results.append({"id": todo_id, "title": title, "ok": True})
        else:
            attempts = int(item.get("attempts", 0)) + 1
            mark_attempt_failed(path, todo_id, attempts, res.get("error", ""))
            results.append({
                "id": todo_id, "title": title, "ok": False,
                "error": res.get("error", ""),
            })
    return results


def queue_status(path: str | None = None) -> dict:
    path = path or queue_path()
    try:
        open_items = list_open(path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "open": len(open_items),
        "items": [
            {"id": t.get("id"), "title": t.get("title"), "when": t.get("when"),
             "list": t.get("list", ""),
             "attempts": t.get("attempts", 0), "last_error": t.get("last_error", "")}
            for t in open_items
        ],
    }


# ---------------------------------------------------------------------------
# CLI (ops/debugging)
# ---------------------------------------------------------------------------

def _cli() -> int:
    import sys
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(json.dumps(queue_status(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "flush":
        print(json.dumps(flush_queue(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "test-push":
        cfg = get_config()
        if not cfg["bark_key"]:
            print("ERROR: THINGS_BARK_KEY not configured")
            return 1
        res = push_bark(
            cfg["bark_key"], cfg["bark_host"],
            "点我进 Things",
            "Hermes → Things 桥接测试",
            build_things_url("Hermes 桥接测试", "today", "来自 things-bridge 自测"),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    if cmd == "add" and len(argv) >= 3:
        res = add_things_todo(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    print("usage: bridge.py [status|flush|test-push|add <title> <when> [notes]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
