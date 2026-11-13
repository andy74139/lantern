#!/bin/zsh
# 登入後自動啟動本機 AI 控制台（macOS LaunchAgent）。
# 用法：./autostart.sh install | uninstall | restart | status
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.ai-controller"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OPEN_LABEL="$LABEL.open"
OPEN_PLIST="$HOME/Library/LaunchAgents/$OPEN_LABEL.plist"
PORT="${PORT:-8765}"
DOMAIN="gui/$(id -u)"

case "$1" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>-m</string><string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
    <string>--no-access-log</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- 控制台重啟或停止時，不要連帶關掉它在背景啟動的長時間工作 -->
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/server.log</string>
</dict>
</plist>
EOF
    # 已經裝過就先卸下；卸載不是立刻完成，要等它真的消失再重新載入
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for i in {1..40}; do
      launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
      sleep 0.25
    done
    launchctl bootstrap "$DOMAIN" "$PLIST"
    # 開機登入後打開首頁（只執行一次，不常駐）
    cat > "$OPEN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$OPEN_LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/open_home.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$ROOT/data/server.log</string>
</dict>
</plist>
EOF
    launchctl bootout "$DOMAIN/$OPEN_LABEL" 2>/dev/null || true
    for i in {1..40}; do
      launchctl print "$DOMAIN/$OPEN_LABEL" >/dev/null 2>&1 || break
      sleep 0.25
    done
    launchctl bootstrap "$DOMAIN" "$OPEN_PLIST"
    echo "已安裝，登入後會自動啟動：http://127.0.0.1:$PORT（開機後也會自動打開首頁）"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootout "$DOMAIN/$OPEN_LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$OPEN_PLIST"
    echo "已移除自動啟動（背景執行中的長時間工作不受影響）。"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "已重新啟動。"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code" || echo "尚未安裝自動啟動。"
    ;;
  *)
    echo "用法：$0 install | uninstall | restart | status"; exit 1 ;;
esac
