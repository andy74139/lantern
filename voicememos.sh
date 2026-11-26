#!/bin/zsh
# 語音備忘錄自動偵測：編譯並安裝同步小幫手（tools/voicememo_sync.swift）。
# 用法：./voicememos.sh install | uninstall | status | run
#   install    編譯小幫手到 bin/voicememo-sync，並設定成每分鐘執行一次（LaunchAgent）
#   uninstall  移除排程（已複製進來的錄音不受影響）
#   status     看小幫手最近一次的結果
#   run        立刻執行一次
# 安裝後要到「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」把 bin/voicememo-sync 加進去，見 README。
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.ai-controller.voicememos"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN="$ROOT/bin/voicememo-sync"
DEST="$ROOT/data/voicememos"
DOMAIN="gui/$(id -u)"

case "$1" in
  install)
    mkdir -p "$ROOT/bin" "$DEST" "$HOME/Library/LaunchAgents"
    # 重新編譯會讓 macOS 認不得它，要重新給一次權限，所以原始碼沒變就不重編
    SUM="$(shasum -a 256 "$ROOT/tools/voicememo_sync.swift" | cut -d' ' -f1)"
    if [[ ! -x "$BIN" || "$(cat "$ROOT/bin/.voicememo-sync.sha" 2>/dev/null)" != "$SUM" ]]; then
      swiftc -O "$ROOT/tools/voicememo_sync.swift" -o "$BIN"
      echo "$SUM" > "$ROOT/bin/.voicememo-sync.sha"
      echo "已編譯：$BIN（如果之前給過權限，要重新加一次）"
    fi
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$BIN</string><string>--dest</string><string>$DEST</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$ROOT/data/server.log</string>
</dict>
</plist>
PL
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for i in {1..40}; do
      launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
      sleep 0.25
    done
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "已安裝：每分鐘檢查一次語音備忘錄。"
    echo "還要給權限：系統設定 → 隱私權與安全性 → 完整磁碟取用權限 → ＋ → 選 $BIN"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已移除排程。也可以到「完整磁碟取用權限」把 voicememo-sync 移除。"
    ;;
  run)
    launchctl kickstart "$DOMAIN/$LABEL" && sleep 2 && cat "$DEST/heartbeat.json"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && echo "排程：已安裝" || echo "排程：沒有安裝"
    cat "$DEST/heartbeat.json" 2>/dev/null || echo "還沒有執行記錄"
    ;;
  *)
    echo "用法：$0 install | uninstall | status | run"; exit 1 ;;
esac
