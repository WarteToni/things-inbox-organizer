# 需在 Mac 上首次运行验证的清单

开发环境是 Linux ECS，AppleScript 无法实测。以下为 Mac 首跑验证点，按顺序执行：

1. 语法编译检查（不执行）:
   python3 -m py_compile things_inbox_organizer.py
2. Things 脚本权限:
   osascript -e 'tell application "Things3" to return name of list "Inbox"'
   弹窗点允许。若报"未授权"，去 系统设置→隐私与安全性→自动化 里勾选。
3. 读取 Inbox（在 things_inbox_organizer.py 里临时 python3 -c 调 read_inbox，
   或直接跑 --dry-run）确认能拿到条目、中文不乱码。
4. LLM 分类连通：填好 API Key 后 --dry-run，看日志里分类结果是否合理
   （日志: ~/Library/Logs/things-organizer.log）。
5. 正式跑一次去掉 --dry-run，到 Things 里确认条目进了正确区域、日期正确。
6. 确认 AppleScript 细节（最可能要微调的地方）:
   a. "scheduling of to-do ... to today/soon/someday" —— Things AppleScript
      正确写法可能是 set schedule of / 或对 to-do 的 when 属性，
      参考官方文档 https://culturedcode.com/things/support/articles/4562654/
      若报错，改为: set scheduling of t to "today" 字符串形式，或用
      "set deadline" 变通。
   b. 区域名字必须与 Things 里完全一致（含括号全角字符），脚本里
      AREAS 字典可按实际区域名修改。
   c. 移动到区域的命令是 move to-do id ... to list "区域名"，若不生效
      试 to-do id ... of list "Inbox" 限定来源。
7. install.sh 跑完后: launchctl list | grep things-inbox-organizer 确认加载；
   /tmp/things-organizer.err 无报错。
8. 单次处理上限 50 条（MAX_ITEMS_PER_RUN），条目多会分多轮自动消化。
