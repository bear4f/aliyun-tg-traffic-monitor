#!/usr/bin/env bash
# Entry point installed as /usr/local/bin/aliyun-monitor.
set -Eeuo pipefail

APP_DIR=/opt/aliyun-traffic-bot
SERVICE=aliyun-traffic-bot
PY="$APP_DIR/venv/bin/python"
PANEL="$APP_DIR/panel.py"

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 root 执行：sudo aliyun-monitor" >&2
  exit 1
fi

if [[ ! -x "$PY" ]]; then
  echo "未找到 $PY，请先运行 install.sh。" >&2
  exit 1
fi

fix_permissions() {
  chown -R aliyunmon:aliyunmon "$APP_DIR"
  chmod 750 "$APP_DIR"
  chmod 600 "$APP_DIR/config.json" "$APP_DIR/state.json" 2>/dev/null || true
}

# Config changes only take effect once the service reloads them.
configure() {
  "$PY" "$PANEL"
  fix_permissions
  if "$PY" "$PANEL" --check; then
    systemctl restart "$SERVICE"
    echo "服务已重启，新配置已生效。"
  else
    echo "配置未通过校验，服务保持原样运行。" >&2
  fi
}

pause() { read -r -p "按回车返回菜单……" _; }

case "${1:-menu}" in
  config|configure|edit) configure; exit 0 ;;
  check)   "$PY" "$PANEL" --check; exit $? ;;
  doctor)  "$PY" "$PANEL" --doctor; exit $? ;;
  status)  "$PY" "$PANEL" --status; systemctl --no-pager --full status "$SERVICE"; exit 0 ;;
  logs)    journalctl -u "$SERVICE" -f ;;
  restart) fix_permissions; systemctl restart "$SERVICE"; systemctl --no-pager --full status "$SERVICE"; exit $? ;;
  start|stop) systemctl "$1" "$SERVICE"; exit $? ;;
  menu) ;;
  -h|--help|help)
    cat <<'EOF'
用法：aliyun-monitor [子命令]

  (无)      打开交互面板
  config    直接进入配置管理
  check     结构校验（不联网）
  doctor    联网自检：配置、服务、Telegram、阿里云 API
  status    状态栏 + systemd 状态
  logs      跟随日志
  restart   修复权限并重启服务
  start     启动服务
  stop      停止服务
EOF
    exit 0 ;;
  *)
    echo "未知子命令：$1（试试 aliyun-monitor --help）" >&2
    exit 2 ;;
esac

while true; do
  clear
  "$PY" "$PANEL" --status || true
  cat <<'EOF'
  1) 管理配置与机器
  2) 一键自检 / 诊断
  3) 查看实时日志
  4) 重启服务
  5) 启动服务
  6) 停止服务
  0) 退出
EOF
  read -r -p "请选择 [1]: " choice
  case "${choice:-1}" in
    1) configure; pause ;;
    2) "$PY" "$PANEL" --doctor || true; pause ;;
    3) journalctl -u "$SERVICE" -f || true ;;
    4) fix_permissions; systemctl restart "$SERVICE"; echo "服务已重启。"; pause ;;
    5) systemctl start "$SERVICE"; echo "服务已启动。"; pause ;;
    6) systemctl stop "$SERVICE"; echo "服务已停止。"; pause ;;
    0) exit 0 ;;
    *) echo "无效选项。"; pause ;;
  esac
done
