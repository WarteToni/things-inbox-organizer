#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Things Inbox 自动归档工具 (macOS)
读取 Things 3 Inbox 待办 -> GLM 抽取(区域 + 日期) -> AppleScript 归档。
用法: python3 things_inbox_organizer.py [--dry-run]

归档规则（2026-09-24 修订）:
- 条目写了明确日期 -> 设到 Things 的 When 字段，并把日期词从标题里删掉（时刻保留）
- "X 前" 这类 deadline 语义 -> 不设 When，日期写进 notes
- 没写日期 -> 只归区域，When 留空（落在区域 Anytime），不猜日期
"""
import json
import subprocess
import sys
import time
import os
import urllib.request
import urllib.error
from datetime import datetime, date

# ---------- 配置 ----------
ENV_PATH = os.path.expanduser("~/.things-organizer/.env")
LOG_PATH = os.path.expanduser("~/Library/Logs/things-organizer.log")
LLM_BASE_URL = "https://open.bigmodel.cn/api/anthropic"   # Coding Plan 专用 Anthropic 协议端点
LLM_MODEL = "glm-5.3"
MAX_ITEMS_PER_RUN = 50      # 单次最多处理条数，防失控
LLM_TIMEOUT = 120           # glm-5.3 思考恒开，留足超时
SHORT_TITLE_LEN = 12        # 短标题不做长度校验
MIN_TITLE_KEEP_RATIO = 0.4  # 长标题至少保留原文这个比例，防 LLM 过度概括
DEADLINE_NOTE_PREFIX = "[截止] "

# 区域体系：LLM 只需返回 key；AppleScript 按名字精确匹配区域。
# 个人区域配置在 ~/.things-organizer/areas.json（不进 git），格式:
#   {"WORK": {"name": "Things里的区域名", "desc": "给LLM看的判断依据"}, ...}
# 未配置时使用下面的内置通用示例。
CONFIG_DIR = os.path.expanduser("~/.things-organizer")
AREAS_PATH = os.path.join(CONFIG_DIR, "areas.json")
DEFAULT_AREAS = {
    "WORK": {"name": "Work", "desc": "工作/公司/项目相关"},
    "FAMILY": {"name": "Family", "desc": "家庭/家人/生活杂事"},
    "PERSONAL": {"name": "Personal", "desc": "个人事务/学习/健康"},
    "IDEAS": {"name": "Ideas", "desc": "想法/调研/将来可能做的事"},
}


def load_areas() -> dict:
    """读取区域配置；文件缺失或损坏时回退内置示例"""
    try:
        with open(AREAS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        areas = {
            str(k).strip().upper(): {"name": str(v["name"]).strip(),
                                     "desc": str(v.get("desc", "")).strip()}
            for k, v in cfg.items()
            if isinstance(v, dict) and str(v.get("name", "")).strip()
        }
        if areas:
            return areas
        log(f"WARN {AREAS_PATH} 为空，使用内置示例区域")
    except FileNotFoundError:
        pass  # 首次运行，正常
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARN 区域配置读取失败({e})，使用内置示例区域")
    return {k: dict(v) for k, v in DEFAULT_AREAS.items()}


AREAS = load_areas()
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

SYSTEM_PROMPT_TMPL = """你是 Things 3 待办归档助手。今天是 {today}（{weekday}）。

对每条待办判断以下四项：

1. area — 归属区域，只能从这些里选:
{areas}
   - "": 拿不准就留空(条目将留在Inbox不动)

2. date — 从标题或 notes 中抽取"这件事该做的日期"，输出 "YYYY-MM-DD"；没有明确日期则输出 ""。
   换算基准：条目创建日（created 字段）；created 为空时用今天 {today}。
   - 绝对日期："9/25"、"9月25日"、"2026-09-25" → 补全为完整日期
   - 相对日期："今天"、"明天"、"后天"、"3天后" → 以创建日为基准换算
   - 星期："周三"、"本周五"、"下周三" → 取创建日起最近的那个周X(含创建日当天)，已过去的顺延到下周；"下周X"取下一个自然周
   - 月粒度："下个月"、"10月交房租" → 取该月 1 日
   - 日期区间（"9/25-9/28 出差"）→ 取开始日
   - 标题里出现多个日期（"9/25 准备，9/28 提交"）→ 取**最早**的那个
   - 事件发生日也算执行日（"9/25 接人" → 2026-09-25）
   - 抽不准就输出 "" —— 宁可留空，绝不猜

3. is_deadline — 该日期是否为"截止/之前"语义（如 "9/18 前发出"、"周五前回复"）。
   true = 这是最晚期限；false = 这是执行日。

4. title — 清理日期词后的标题。
   **注意：title 怎么处理不影响第 2 项 —— 无论下面哪条，date 都必须照常抽取。**
   只有"标题里唯一的日期就是 date 字段抽到的那个"时才删它，否则标题原样返回。
   - 标题不含日期 → 原样返回
   - 标题含多个日期或日期区间（"9/25 准备，9/28 提交"）→ 原样返回，不要删，避免丢信息
   - 可删的时间词："9/25"、"9月25日"、"明日"、"周三"、"下周"、"今天"、"3天后"
   - 保留：具体时刻（"18:15"、"14:52 到深圳北站"）、车次、地点、人名、数字等实质信息
   - 删完不通顺就微调措辞
   - 不要精简概括 —— 保持原标题的全部关键信息

输出严格 JSON 数组，每项: {{"idx": <序号>, "area": "<key或空串>", "date": "<YYYY-MM-DD或空串>", "is_deadline": <true|false>, "title": "<清理后的标题>"}}
不要输出任何其他文字。"""


def log(msg: str):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def load_api_key() -> str:
    if not os.path.exists(ENV_PATH):
        os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("# Things Inbox Organizer 配置\n# 在 https://open.bigmodel.cn 获取 API Key\n"
                    "THINGSORG_API_KEY=在这里粘贴你的APIKey\n")
        log(f"ERROR 未找到配置，已创建示例 {ENV_PATH}，请填入 THINGSORG_API_KEY 后重跑")
        print(f"首次运行：已创建示例配置 {ENV_PATH}，请填入 API Key 后重新运行。")
        sys.exit(2)
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("THINGSORG_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key and "粘贴" not in key:
                    return key
    log("ERROR .env 存在但 THINGSORG_API_KEY 未填写")
    print(f"请编辑 {ENV_PATH}，填入 THINGSORG_API_KEY。")
    sys.exit(2)


def run_osascript(script: str) -> str:
    """执行 AppleScript，返回 stdout（出错抛异常）"""
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"osascript 失败: {p.stderr.strip()}")
    return p.stdout.strip()


def esc(s: str) -> str:
    """转义 AppleScript 字符串中的引号和反斜杠"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def read_inbox() -> list:
    """读取 Inbox 全部待办: [{idx,id,name,notes,created}]，created 为 YYYY-MM-DD"""
    script = r'''
    set D1 to character id 1
    set D2 to character id 2
    tell application "Things3"
        set out to ""
        repeat with t in to dos of list "Inbox"
            set nt to notes of t
            if nt is missing value then set nt to ""
            set cd to creation date of t
            if cd is missing value then
                set cds to ""
            else
                set cds to ((year of cd) as string) & "-" & text -2 thru -1 of ("0" & ((month of cd) as integer) as string) & "-" & text -2 thru -1 of ("0" & (day of cd) as string)
            end if
            set out to out & (id of t) & D1 & (name of t) & D1 & nt & D1 & cds & D2
        end repeat
        return out
    end tell
    '''
    raw = run_osascript(script)
    items = []
    idx = 0
    for rec in raw.split("\u0002"):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split("\u0001")
        if len(parts) < 3:
            continue
        items.append({
            "idx": idx,
            "id": parts[0],
            "name": parts[1],
            "notes": parts[2],
            "created": parts[3] if len(parts) > 3 else "",
        })
        idx += 1
    return items


def normalize_date(s: str) -> str:
    """校验 YYYY-MM-DD；非法或早于今天的日期返回空串（此时不设 When，日期留在标题里）"""
    if not s:
        return ""
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        log(f'WARN LLM 返回非法日期 "{s}"，忽略')
        return ""
    if d < date.today():
        log(f"WARN 日期 {s} 已过期，不设 When（日期保留在标题里）")
        return ""
    return d.isoformat()


def area_lines(areas: dict) -> str:
    """把区域配置渲染成 prompt 行"""
    return "\n".join(f'   - "{k}": {v["desc"]}' for k, v in areas.items())


def classify(items: list, api_key: str) -> dict:
    """调 GLM 抽取区域与日期，返回 {idx: {area, date, is_deadline, title}}"""
    today = date.today()
    prompt = SYSTEM_PROMPT_TMPL.format(
        today=today.isoformat(),
        weekday=WEEKDAY_CN[today.weekday()],
        areas=area_lines(AREAS),
    )
    payload_items = [
        {"idx": it["idx"], "title": it["name"][:120], "notes": it["notes"][:200],
         "created": it["created"]}
        for it in items[:MAX_ITEMS_PER_RUN]
    ]
    body = json.dumps({
        "model": LLM_MODEL,
        "system": prompt,
        "messages": [
            {"role": "user", "content": "待办列表:\n" + json.dumps(payload_items, ensure_ascii=False)},
        ],
        # glm-5.3 思考恒开；不传 temperature（思考开启时只允许 1）
        "max_tokens": 8192,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLM_BASE_URL + "/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # Anthropic 协议: content 是块列表，拼接其中的 text 块（thinking 块自动跳过）
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    # 剥掉可能的 ```json 包裹
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    start, end = text.find("["), text.rfind("]")
    arr = json.loads(text[start:end + 1])
    result = {}
    for r in arr:
        try:
            idx = int(r["idx"])
            area = str(r.get("area", "") or "").strip().upper()
            if area not in AREAS:
                area = ""
            result[idx] = {
                "area": area,
                "date": normalize_date(str(r.get("date", "") or "").strip()),
                "is_deadline": bool(r.get("is_deadline", False)),
                "title": str(r.get("title", "") or "").strip(),
            }
        except (KeyError, ValueError, TypeError):
            continue  # 单条解析失败 -> 留 Inbox
    return result


def is_safe_rewrite(old: str, new: str) -> bool:
    """防 LLM 过度概括：短标题只要求非空，长标题要求保留足够长度"""
    if not new or new == old:
        return False
    if len(old) <= SHORT_TITLE_LEN:
        return True
    return len(new) >= len(old) * MIN_TITLE_KEEP_RATIO


def schedule_to_date(tid: str, d: str) -> None:
    """把待办 schedule 到指定日期（YYYY-MM-DD）"""
    y, m, dd = (int(x) for x in d.split("-"))
    script = '''tell application "Things3"
set theDate to (current date)
set time of theDate to 0
set day of theDate to 1
set year of theDate to {y}
set month of theDate to {m}
set day of theDate to {dd}
schedule to do id "{tid}" for theDate
end tell'''.format(y=y, m=m, dd=dd, tid=tid)
    run_osascript(script)


def append_note(tid: str, line: str) -> None:
    """在 notes 末尾追加一行（notes 为空则直接写入）"""
    script = '''tell application "Things3"
set t to to do id "{tid}"
set n to notes of t
if n is missing value then set n to ""
if n is "" then
set notes of t to "{line}"
else
set notes of t to n & return & "{line}"
end if
end tell'''.format(tid=tid, line=esc(line))
    run_osascript(script)


def apply_decision(item: dict, dec: dict) -> str:
    """移入区域 + 设 When（或 deadline 写 notes）+ 清理标题，返回描述"""
    tid = item["id"]
    area_name = AREAS[dec["area"]]["name"]
    d = dec["date"]
    acts = []

    # 1. 移入区域（区域可经 list "区域名" 寻址，实测可移入）
    run_osascript(
        'tell application "Things3" to move to do id "{}" to list "{}"'.format(tid, esc(area_name))
    )
    acts.append(area_name)

    # 2. 日期：执行日 -> When；deadline -> notes；无日期 -> 不设（落 Anytime）
    if d and dec["is_deadline"]:
        append_note(tid, DEADLINE_NOTE_PREFIX + d)
        acts.append(f"deadline→notes({d})")
    elif d:
        schedule_to_date(tid, d)
        acts.append(f"when={d}")
    else:
        acts.append("无日期→Anytime")

    # 3. 标题清理（仅在日期已落位后，避免信息丢失）
    if d and is_safe_rewrite(item["name"], dec["title"]):
        run_osascript(
            'tell application "Things3" to set name of to do id "{}" to "{}"'.format(
                tid, esc(dec["title"]))
        )
        acts.append("标题已清理")

    return '"{}" -> {}'.format(item["name"][:60], " | ".join(acts))


def describe(item: dict, dec: dict) -> str:
    """dry-run 预览：人话描述将要执行的动作"""
    tag = AREAS[dec["area"]]["name"] + " | " + (dec["date"] or "无日期→Anytime")
    if dec["date"] and dec["is_deadline"]:
        tag += " (deadline→notes)"
    line = '"{}" -> {}'.format(item["name"][:60], tag)
    if dec["date"] and is_safe_rewrite(item["name"], dec["title"]):
        line += ' | 标题改为: "{}"'.format(dec["title"][:60])
    return line


def net_ok() -> bool:
    try:
        req = urllib.request.Request(LLM_BASE_URL + "/v1/messages", method="GET")
        urllib.request.urlopen(req, timeout=8)
        return True
    except urllib.error.HTTPError:
        return True  # 有HTTP响应说明网络可达(404/405等也算通)
    except Exception:
        return False


def main():
    dry = "--dry-run" in sys.argv
    log(f"--- 运行开始 (dry_run={dry}) ---")
    if not net_ok():
        log("网络不可达(open.bigmodel.cn)，跳过本次")
        return
    api_key = load_api_key()
    items = read_inbox()
    log(f"Inbox 共 {len(items)} 条")
    if not items:
        log("Inbox 为空，结束")
        return
    try:
        decisions = classify(items, api_key)
    except Exception as e:
        log(f"ERROR LLM 分类失败: {e}")
        return
    moved = kept = 0
    for it in items:
        dec = decisions.get(it["idx"])
        if not dec or not dec["area"]:
            if dry and dec:
                log('[dry-run] 保留Inbox(area拿不准): "{}" | 抽到日期={}'.format(
                    it["name"][:50], dec["date"] or "无"))
            else:
                log('保留 Inbox: "{}"'.format(it["name"][:60]))
            kept += 1
            continue
        if dry:
            log("[dry-run] " + describe(it, dec))
            continue
        try:
            log("归档: " + apply_decision(it, dec))
            moved += 1
            time.sleep(0.3)  # 给 Things 喘息
        except Exception as e:
            log('ERROR 归档失败 "{}": {} (留在Inbox)'.format(it["name"][:40], e))
    log(f"--- 运行结束: 归档 {moved} 条, 保留 {kept} 条 ---")


if __name__ == "__main__":
    main()
