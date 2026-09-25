#!/bin/bash
# Things Inbox Organizer - 安装脚本 (macOS)
# 项目现在常驻 ~/Projects/things-organizer/，launchd 直接运行源码，
# 不再有 /usr/local/bin 副本（单一数据源，改完即生效）。
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST=com.things-inbox-organizer.launchd.plist
LABEL=com.things-inbox-organizer

# 1. 脚本可执行 + 清除 quarantine（launchd 拒载带隔离属性的文件，报 error 5）
chmod +x "$DIR/things_inbox_organizer.py"
xattr -d com.apple.quarantine "$DIR/$PLIST" 2>/dev/null || true

# 2. 安装 LaunchAgent（把 plist 里的脚本路径替换为实际位置）
mkdir -p ~/Library/LaunchAgents
sed "s|__INSTALL_DIR__|$DIR|" \
    "$DIR/$PLIST" > ~/Library/LaunchAgents/$PLIST
xattr -d com.apple.quarantine ~/Library/LaunchAgents/$PLIST 2>/dev/null || true

# 3. 加载（先卸旧的，忽略未加载报错）
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$PLIST

# 4. 日志目录
mkdir -p ~/Library/Logs
touch ~/Library/Logs/things-organizer.log

echo ""
echo "=== 安装完成 ==="
echo "源码位置: $DIR"
echo "定时: 8:00-22:00 每 2 小时；日志: ~/Library/Logs/things-organizer.log"
echo "API Key 在 ~/.things-organizer/.env（若未填，跑一次脚本会生成示例）"
