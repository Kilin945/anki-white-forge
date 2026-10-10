#!/usr/bin/env bash
# 讓 Mac 工作機登入後自動啟動（launchd）。重跑會覆蓋舊設定。
# 移除：launchctl bootout gui/$(id -u)/local.anki-jenkins-agent
#       再刪掉 ~/Library/LaunchAgents/local.anki-jenkins-agent.plist
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; source "$DIR/.env"; set +a

LABEL="local.anki-jenkins-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# launchd 的 PATH 只有系統目錄；建置要用到 uv，把它的目錄帶進去
UV_DIR="$(dirname "$(command -v uv)")"
mkdir -p "$MAC_AGENT_WORKDIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/start-agent.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$UV_DIR:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- 主控台沒開（Docker 沒啟動）時腳本會失敗退出，60 秒後再試 -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$MAC_AGENT_WORKDIR/agent.log</string>
  <key>StandardErrorPath</key><string>$MAC_AGENT_WORKDIR/agent.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (log: $MAC_AGENT_WORKDIR/agent.log)"
