#!/bin/zsh
# 開機登入後打開首頁（由 LaunchAgent local.ai-controller.open 執行）。
# 只在開機後 10 分鐘內執行，所以安裝或重新登入時不會多開分頁。
# 不想自動開啟：把 config/home.json 的 "open_on_login" 改成 false。
cd "$(dirname "$0")" || exit 1
URL="http://localhost:${PORT:-8765}/"   # 要用 localhost：YouTube 不允許在 IP 網址內嵌播放

boot=$(sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+),.*/\1/')
(( $(date +%s) - boot > 600 )) && [[ "$1" != "--force" ]] && exit 0

enabled=$(.venv/bin/python -c "import json; print(json.load(open('config/home.json')).get('open_on_login', True))" 2>/dev/null)
[[ "$enabled" == "False" ]] && exit 0

for i in {1..60}; do                      # 等服務起來（最多 60 秒）
  curl -s -o /dev/null -m 2 "$URL" && break
  sleep 1
done
open "$URL"
