#!/usr/bin/env bash
# deploy.sh - Install things-bridge plugin into ~/.hermes/plugins/ and enable it.
# Idempotent; safe to re-run after code changes (plugin hot-reloads on gateway restart).
set -euo pipefail

SRC="/root/Tech_Lab/projects/hermes-things-bridge/plugin"
DST="/root/.hermes/plugins/things-bridge"
PYTHON="/usr/local/lib/hermes-agent-v020/venv/bin/python"

echo "==> 1/4 syntax check"
"$PYTHON" -m py_compile "$SRC/bridge.py" "$SRC/__init__.py"

echo "==> 2/4 copy plugin files -> $DST"
mkdir -p "$DST"
cp "$SRC/plugin.yaml" "$SRC/__init__.py" "$SRC/bridge.py" "$DST/"

echo "==> 3/4 verify import (plugin register against a mock ctx)"
"$PYTHON" - "$DST" <<'EOF'
import importlib.util, sys, types
from pathlib import Path
dst = Path(sys.argv[1])
# Register a fake package first so relative imports resolve.
pkg = types.ModuleType("tb_test")
pkg.__path__ = [str(dst)]
sys.modules["tb_test"] = pkg
b_spec = importlib.util.spec_from_file_location("tb_test.bridge", dst / "bridge.py")
b_mod = importlib.util.module_from_spec(b_spec)
sys.modules["tb_test.bridge"] = b_mod
b_spec.loader.exec_module(b_mod)
spec = importlib.util.spec_from_file_location("tb_test", dst / "__init__.py",
    submodule_search_locations=[str(dst)])
mod = importlib.util.module_from_spec(spec)
sys.modules["tb_test"] = mod
mod.__path__ = [str(dst)]
spec.loader.exec_module(mod)

calls = []
class MockCtx:
    def register_tool(self, **kw): calls.append(kw["name"])
mod.register(MockCtx())
assert "add_things_todo" in calls and "things_bridge_status" in calls, calls
print("register() OK, tools:", calls)
print("bark key configured:", b_mod.is_configured())
EOF

echo "==> 4/4 enable plugin in config.yaml (idempotent)"
if ! grep -q "things-bridge" /root/.hermes/config.yaml; then
  "$PYTHON" - <<'EOF'
import re
p = "/root/.hermes/config.yaml"
s = open(p, encoding="utf-8").read()
# insert under plugins.enabled list
pat = re.compile(r"(plugins:\n\s+disabled: \[\]\n\s+enabled:\n)")
assert pat.search(s), "plugins.enabled section not found"
s = pat.sub(r"\1    - things-bridge\n", s, count=1)
open(p, "w", encoding="utf-8").write(s)
print("added things-bridge to plugins.enabled")
EOF
else
  echo "already enabled, skipping"
fi

echo "==> done. Restart the gateway to activate: /restart or systemctl restart."
