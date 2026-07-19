#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/aliyun-traffic-bot
SERVICE_NAME=aliyun-traffic-bot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 root 执行。" >&2
  exit 1
fi

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ca-certificates
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 py3-pip py3-virtualenv ca-certificates shadow tzdata
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip ca-certificates shadow-utils
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip ca-certificates shadow-utils
  else
    echo "不支持的包管理器，请手动安装 Python 3.9+ 与 venv。" >&2
    exit 1
  fi
}

install_packages

# zoneinfo and the SDK both need 3.9+; fail here rather than mid-pip.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "需要 Python 3.9 或更高版本，当前为 $(python3 -V 2>&1)。" >&2
  exit 1
fi

getent group aliyunmon >/dev/null 2>&1 || groupadd --system aliyunmon
id aliyunmon >/dev/null 2>&1 || useradd --system --gid aliyunmon --home-dir "$APP_DIR" --shell /usr/sbin/nologin aliyunmon
mkdir -p "$APP_DIR"

if [[ -f "$APP_DIR/config.json" ]]; then
  cp -a "$APP_DIR/config.json" "$APP_DIR/config.json.backup.$(date +%Y%m%d-%H%M%S)"
  echo "已保留原配置并创建时间戳备份。"
fi

install -m 0755 "$SRC_DIR/app.py"    "$APP_DIR/app.py"
install -m 0644 "$SRC_DIR/common.py" "$APP_DIR/common.py"
install -m 0755 "$SRC_DIR/panel.py"  "$APP_DIR/panel.py"
install -m 0644 "$SRC_DIR/config.example.json" "$APP_DIR/config.example.json"
install -m 0644 "$SRC_DIR/requirements.txt"    "$APP_DIR/requirements.txt"
install -m 0755 "$SRC_DIR/manage.sh" "$APP_DIR/manage.sh"
install -m 0644 "$SRC_DIR/ram-policy-ecs-cdt.json" "$APP_DIR/ram-policy-ecs-cdt.json"
install -m 0644 "$SRC_DIR/ram-policy-swas.json"    "$APP_DIR/ram-policy-swas.json"

# 2.x shipped the terminal panel as setup.py; drop it so stale bytecode and a
# module named setup.py cannot shadow the new entry point.
rm -f "$APP_DIR/setup.py"
rm -rf "$APP_DIR/__pycache__"

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install --requirement "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/config.json" ]]; then
  "$APP_DIR/venv/bin/python" "$APP_DIR/panel.py" --init
else
  echo "检测到已有 config.json，升级时不会覆盖。"
  "$APP_DIR/venv/bin/python" "$APP_DIR/panel.py" --check || true
fi

chown -R aliyunmon:aliyunmon "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 600 "$APP_DIR/config.json" "$APP_DIR/state.json" 2>/dev/null || true
install -m 0644 "$SRC_DIR/aliyun-traffic-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
ln -sfn "$APP_DIR/manage.sh" /usr/local/bin/aliyun-monitor
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

cat <<EOF

安装/升级完成。

  交互面板：aliyun-monitor
  一键自检：aliyun-monitor doctor
  服务状态：systemctl status ${SERVICE_NAME}
  实时日志：journalctl -u ${SERVICE_NAME} -f

在 Telegram 里给你的 Bot 发送 /menu 打开控制面板。
EOF
