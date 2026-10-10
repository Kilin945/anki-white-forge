#!/usr/bin/env bash
# 在 Mac 上啟動 Jenkins 工作機，連到 Docker 裡的主控台。
# 用法：先 docker compose up -d，再在這個資料夾執行 ./start-agent.sh
set -euo pipefail
cd "$(dirname "$0")"
set -a; source .env; set +a

URL="http://localhost:18080"
mkdir -p "$MAC_AGENT_WORKDIR"
JAR="$MAC_AGENT_WORKDIR/agent.jar"

# agent.jar 跟著主控台版本走，每次啟動都重抓
curl -fsS -o "$JAR" "$URL/jnlpJars/agent.jar"

# 工作機的連線密鑰由主控台產生，不存檔
SECRET=$(curl -fsS -u "$JENKINS_ADMIN_ID:$JENKINS_ADMIN_PASSWORD" \
  "$URL/computer/mac/jenkins-agent.jnlp" \
  | sed -n 's:.*<application-desc><argument>\([^<]*\)</argument>.*:\1:p')

# 這版 Jenkins 要求工作機用 Java 21；只在這裡指定，不動 Mac 的預設 Java
JAVA="$(/usr/libexec/java_home -v 21)/bin/java"

exec "$JAVA" -jar "$JAR" -url "$URL" -name mac -secret "$SECRET" \
  -webSocket -workDir "$MAC_AGENT_WORKDIR"
