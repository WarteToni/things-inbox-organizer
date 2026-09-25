#!/usr/bin/env python3
"""test_bridge.py - Full test suite for the things-bridge core logic.

Covers: date normalization (Chinese), Things URL construction, queue
persistence/locking, Bark push with retry against a local mock HTTP
server, end-to-end add_things_todo flow.

Run:  /usr/local/lib/hermes-agent-v020/venv/bin/python test_bridge.py
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "plugin"))

import bridge  # noqa: E402

PASS = 0
FAIL = 0
TODAY = dt.date(2026, 8, 11)  # Tuesday


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def test_normalize_when():
    print("\n[test_normalize_when]")
    n = bridge.normalize_when
    check("ISO passthrough", n("2026-08-14") == "2026-08-14")
    check("ISO invalid rejected", n("2026-13-40") is None)
    check("keyword today", n("today") == "today")
    check("keyword TOMORROW case", n("TOMORROW") == "tomorrow")
    check("今天", n("今天", TODAY) == "today")
    check("明天", n("明天", TODAY) == "tomorrow")
    check("后天", n("后天", TODAY) == "2026-08-13")
    # 2026-08-11 is Tuesday (weekday=1)
    check("周五 this week", n("周五", TODAY) == "2026-08-14")
    check("周五 today-same-day stays", n("周二", TODAY) == "2026-08-11")
    check("下周一", n("下周一", TODAY) == "2026-08-17")
    check("下周五", n("下周五", TODAY) == "2026-08-21")
    check("星期三", n("星期三", TODAY) == "2026-08-12")
    check("礼拜日", n("礼拜日", TODAY) == "2026-08-16")
    check("8月20号", n("8月20号", TODAY) == "2026-08-20")
    check("1月2日 rolls to next year", n("1月2日", TODAY) == "2027-01-02")
    check("月底", n("月底", TODAY) == "2026-08-31")
    check("月底 December", n("月底", dt.date(2026, 12, 15)) == "2026-12-31")
    check("garbage rejected", n("下周某个时候") is None)
    check("empty rejected", n("") is None)
    check("None rejected", n(None) is None)


def test_build_url():
    print("\n[test_build_url]")
    url = bridge.build_things_url("周五给老板发周报", "2026-08-14", "来自Hermes")
    check("scheme", url.startswith("things:///add?"))
    check("title encoded", "title=%E5%91%A8%E4%BA%94%E7%BB%99%E8%80%81%E6%9D%BF%E5%8F%91%E5%91%A8%E6%8A%A5" in url)
    check("when plain", "when=2026-08-14" in url)
    check("notes encoded", "notes=%E6%9D%A5%E8%87%AAHermes" in url)
    url2 = bridge.build_things_url("test", "today", "")
    check("no notes param when empty", "notes=" not in url2)
    # special chars must be escaped
    url3 = bridge.build_things_url("a&b=c 100%", "today")
    check("ampersand escaped", "a%26b%3Dc%20100%25" in url3)
    # list param
    url4 = bridge.build_things_url("测试", "today", "", "Ginkgo Pharma")
    check("list encoded in url", "list=%E4%BF%9D%E4%B9%90%E7%94%9F%E7%89%A9%E5%8C%BB%E8%8D%AF" in url4)
    url5 = bridge.build_things_url("测试", "today", "")
    check("no list param when empty", "list=" not in url5)


def test_queue(tmpdir: str):
    print("\n[test_queue]")
    path = os.path.join(tmpdir, "todos.json")
    tid = bridge.enqueue_todo(path, "测试任务", "2026-08-14", "备注")
    check("enqueue returns id", bool(tid))
    open_items = bridge.list_open(path)
    check("one open item", len(open_items) == 1)
    check("fields intact", open_items[0]["title"] == "测试任务" and open_items[0]["when"] == "2026-08-14")
    # dedupe
    tid2 = bridge.enqueue_todo(path, "测试任务", "2026-08-14", "备注")
    check("dedupe same title+when", tid2 == tid and len(bridge.list_open(path)) == 1)
    tid3 = bridge.enqueue_todo(path, "另一个任务", "today", "")
    check("different task gets new id", tid3 != tid and len(bridge.list_open(path)) == 2)
    # mark imported
    check("mark_imported", bridge.mark_imported(path, tid, '{"code":200}') is True)
    check("imported no longer open", len(bridge.list_open(path)) == 1)
    # mark failed
    bridge.mark_attempt_failed(path, tid3, 1, "HTTP 500")
    item = bridge.list_open(path)[0]
    check("failure recorded", item["attempts"] == 1 and "500" in item["last_error"])
    # file survives and is valid JSON with _说明
    data = json.load(open(path, encoding="utf-8"))
    check("_说明 preserved", "_说明" in data)


# ---- mock Bark server ----

class BarkHandler(http.server.BaseHTTPRequestHandler):
    responses = []  # class-level capture
    fail_until = 0  # fail first N requests with 500
    _count = 0

    def do_POST(self):
        BarkHandler._count += 1
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        path = self.path  # /<device_key>
        BarkHandler.responses.append({"path": path, "body": body})
        if BarkHandler._count <= BarkHandler.fail_until:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server error")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"code": 200, "message": "success"}).encode())

    def log_message(self, *a):
        pass


def start_mock_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BarkHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_bark_push_and_e2e(tmpdir: str):
    print("\n[test_bark_push + end-to-end]")
    server, host = start_mock_server()
    queue_file = os.path.join(tmpdir, "todos.json")
    env_file = os.path.join(tmpdir, ".env")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f"THINGS_BARK_KEY=testkey123\nTHINGS_BARK_HOST={host}\n")

    old_backoff = bridge.BARK_BACKOFF_BASE
    bridge.BARK_BACKOFF_BASE = 0.01  # fast retries for tests

    # 1) simple successful push
    BarkHandler.responses.clear()
    res = bridge.push_bark("k", host, "t", "b", "things:///add?title=x")
    check("push ok", res["ok"] is True)
    check("one request", len(BarkHandler.responses) == 1)

    # 2) e2e: add_things_todo with Chinese relative date
    BarkHandler.responses.clear()
    res = bridge.add_things_todo(
        "给老板发周报", "周五", "来自Hermes",
        queue_file=queue_file, env_file=env_file,
    )
    check("e2e ok", res["ok"] is True, str(res))
    req = BarkHandler.responses[-1]
    check("bark path has key", req["path"] == "/testkey123")
    check("bark url field is things URL", req["body"]["url"].startswith("things:///add?"))
    check("title encoded in bark url", "%E5%91%A8%E6%8A%A5" in req["body"]["url"])
    check("body shows title", "给老板发周报" in req["body"]["body"])
    check("level timeSensitive", req["body"].get("level") == "timeSensitive")
    check("queue empty after success", len(bridge.list_open(queue_file)) == 0)

    # 3) retry on failure then success
    BarkHandler.responses.clear()
    BarkHandler._count = 0
    BarkHandler.fail_until = 2  # first 2 attempts fail, 3rd succeeds
    res = bridge.push_bark("k", host, "t", "retry-body", "u")
    check("retry recovers", res["ok"] is True and res["attempts"] == 3)
    BarkHandler.fail_until = 0

    # 4) permanent failure: item stays in queue with error
    BarkHandler._count = 0
    BarkHandler.fail_until = 999
    res = bridge.add_things_todo(
        "失败测试任务", "today", "",
        queue_file=queue_file, env_file=env_file,
    )
    check("permanent fail reports not ok", res["ok"] is False)
    check("fail mentions queue/retry", "队列" in res["error"] or "重试" in res["error"])
    check("item preserved in queue", len(bridge.list_open(queue_file)) == 1)
    BarkHandler.fail_until = 0

    # 5) flush recovers the stuck item
    BarkHandler._count = 0
    flushed = bridge.flush_queue(queue_file, env_file=env_file)
    check("flush succeeds", len(flushed) == 1 and flushed[0]["ok"] is True)
    check("queue drained", len(bridge.list_open(queue_file)) == 0)

    # 6) missing key -> clean error, no crash
    res = bridge.add_things_todo("x", "today", "", queue_file=queue_file,
                                 env_file=os.path.join(tmpdir, "nonexistent.env"))
    check("missing key clean error", res["ok"] is False and "THINGS_BARK_KEY" in res["error"])

    # 7) bad date -> clean error
    res = bridge.add_things_todo("x", "瞎写的日期", "", queue_file=queue_file, env_file=env_file)
    check("bad date clean error", res["ok"] is False and "YYYY-MM-DD" in res["error"])

    # 8) empty title -> clean error
    res = bridge.add_things_todo("  ", "today", "", queue_file=queue_file, env_file=env_file)
    check("empty title clean error", res["ok"] is False)

    bridge.BARK_BACKOFF_BASE = old_backoff
    server.shutdown()


def test_classify_todo_list():
    print("\n[test_classify_todo_list]")
    c = bridge.classify_todo_list
    # Acceptance case 1: 巴基斯坦客户报价 -> Ginkgo Pharma
    check("巴基斯坦客户→Ginkgo", c("给巴基斯坦客户发报价") == "Ginkgo Pharma")
    # Acceptance case 2: 带爸妈体检 -> Family
    check("爸妈体检→Family", c("带爸妈体检") == "Family")
    # Acceptance case 3: 买牛奶 -> no match (inbox)
    check("买牛奶→无归类", c("买牛奶") == "")
    # IVD/Acme keywords
    check("IVD→Acme", c("IVD 产品注册") == "Acme IVD")
    check("acme lowercase", c("acme 会议") == "Acme IVD")
    check("体外诊断→Acme", c("体外诊断试剂") == "Acme IVD")
    check("NGS→Acme", c("NGS 建库") == "Acme IVD")
    # Ginkgo keywords
    check("原料药→Ginkgo", c("原料药报价") == "Ginkgo Pharma")
    check("API→Ginkgo", c("API 询价") == "Ginkgo Pharma")
    check("客户→Ginkgo", c("客户拜访") == "Ginkgo Pharma")
    check("询价→Ginkgo", c("询价回复") == "Ginkgo Pharma")
    check("俄罗斯→Ginkgo", c("俄罗斯客户") == "Ginkgo Pharma")
    check("海外客户→Ginkgo", c("海外客户跟进") == "Ginkgo Pharma")
    # 爸妈 keywords
    check("父母→Family", c("父母生日") == "Family")
    check("健康→Family", c("健康检查") == "Family")
    check("老家→Family", c("回老家") == "Family")
    # 个人 keywords
    check("学习→个人", c("学习 Python") == "Personal")
    check("健身→个人", c("健身计划") == "Personal")
    check("理财→个人", c("理财规划") == "Personal")
    # notes also matched
    check("notes 匹配", c("跟进", "巴基斯坦的客户") == "Ginkgo Pharma")
    # explicit title with multiple matches -> first rule wins (dict order)
    check("Acme+Ginkgo→先匹配Acme", c("AcmeGinkgo") == "Acme IVD")
    # empty
    check("空标题", c("") == "")


def test_add_todo_classification(tmpdir: str):
    """Acceptance criteria: end-to-end classification in add_things_todo."""
    print("\n[test_add_todo_classification]")
    server, host = start_mock_server()
    queue_file = os.path.join(tmpdir, "todos_cls.json")
    env_file = os.path.join(tmpdir, ".env")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f"THINGS_BARK_KEY=testkey123\nTHINGS_BARK_HOST={host}\n")

    old_backoff = bridge.BARK_BACKOFF_BASE
    bridge.BARK_BACKOFF_BASE = 0.01

    # AC1: 给巴基斯坦客户发报价 -> list=Ginkgo Pharma
    BarkHandler.responses.clear()
    res = bridge.add_things_todo(
        "给巴基斯坦客户发报价", "today", "",
        queue_file=queue_file, env_file=env_file,
    )
    check("AC1 ok", res["ok"] is True, str(res))
    check("AC1 list=Ginkgo", res.get("list") == "Ginkgo Pharma", str(res.get("list")))
    check("AC1 classified=True", res.get("classified") is True)
    req = BarkHandler.responses[-1]
    check("AC1 url has list param", "list=%E4%BF%9D%E4%B9%90%E7%94%9F%E7%89%A9%E5%8C%BB%E8%8D%AF" in req["body"]["url"], req["body"]["url"])

    # AC2: 带爸妈体检 -> list=Family
    BarkHandler.responses.clear()
    res = bridge.add_things_todo(
        "带爸妈体检", "tomorrow", "",
        queue_file=queue_file, env_file=env_file,
    )
    check("AC2 ok", res["ok"] is True, str(res))
    check("AC2 list=Family", res.get("list") == "Family", str(res.get("list")))
    req = BarkHandler.responses[-1]
    check("AC2 url has list param", "list=%E8%80%81%E7%88%B8%E8%80%81%E5%A6%88%E6%98%AF%E5%A4%A9%E6%98%AF%E5%9C%B0" in req["body"]["url"], req["body"]["url"])

    # AC3: 买牛奶 -> no list (inbox)
    BarkHandler.responses.clear()
    res = bridge.add_things_todo(
        "买牛奶", "today", "",
        queue_file=queue_file, env_file=env_file,
    )
    check("AC3 ok", res["ok"] is True, str(res))
    check("AC3 list empty", res.get("list") == "", str(res.get("list")))
    req = BarkHandler.responses[-1]
    check("AC3 url no list param", "list=" not in req["body"]["url"], req["body"]["url"])

    # AC4: explicit list wins over dictionary
    BarkHandler.responses.clear()
    res = bridge.add_things_todo(
        "AcmeGinkgo都提到", "today", "",
        list_name="Acme IVD",
        queue_file=queue_file, env_file=env_file,
    )
    check("AC4 ok", res["ok"] is True, str(res))
    check("AC4 explicit list wins", res.get("list") == "Acme IVD", str(res.get("list")))
    check("AC4 classified=False", res.get("classified") is False)
    req = BarkHandler.responses[-1]
    check("AC4 url has explicit list", "list=Acme%20%E8%8F%B2%E9%B9%8F" in req["body"]["url"], req["body"]["url"])

    # AC5: explicit list with no keyword match at all
    BarkHandler.responses.clear()
    res = bridge.add_things_todo(
        "买牛奶", "today", "",
        list_name="Personal",
        queue_file=queue_file, env_file=env_file,
    )
    check("AC5 explicit list on no-match", res.get("list") == "Personal", str(res.get("list")))

    bridge.BARK_BACKOFF_BASE = old_backoff
    server.shutdown()


def test_env_parsing(tmpdir: str):
    print("\n[test_env_parsing]")
    env_file = os.path.join(tmpdir, ".env")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("# comment\nTHINGS_BARK_KEY=\"quotedkey\"\nTHINGS_BARK_HOST=https://my.bark.host/\nOTHER=x\n")
    os.environ.pop("THINGS_BARK_KEY", None)
    os.environ.pop("THINGS_BARK_HOST", None)
    cfg = bridge.get_config(env_file)
    check("quoted key stripped", cfg["bark_key"] == "quotedkey")
    check("host trailing slash stripped", cfg["bark_host"] == "https://my.bark.host")
    check("default host when absent", bridge.get_config(os.path.join(tmpdir, "nope"))["bark_host"] == bridge.DEFAULT_BARK_HOST)
    # env var wins over file
    os.environ["THINGS_BARK_KEY"] = "envkey"
    check("env var wins", bridge.get_config(env_file)["bark_key"] == "envkey")
    os.environ.pop("THINGS_BARK_KEY")


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_normalize_when()
        test_build_url()
        test_classify_todo_list()
        test_queue(tmpdir)
        test_bark_push_and_e2e(tmpdir)
        test_add_todo_classification(tmpdir)
        test_env_parsing(tmpdir)
    print(f"\n{'='*40}\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
