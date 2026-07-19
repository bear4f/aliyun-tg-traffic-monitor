#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${EUID} -ne 0 ]]; then echo "请使用 root 执行。" >&2; exit 1; fi
systemctl disable --now aliyun-traffic-bot 2>/dev/null || true
rm -f /etc/systemd/system/aliyun-traffic-bot.service /usr/local/bin/aliyun-monitor
systemctl daemon-reload
read -r -p "是否删除 /opt/aliyun-traffic-bot（含密钥、配置与状态）？[y/N]: " answer
if [[ ${answer:-N} =~ ^[Yy]$ ]]; then rm -rf /opt/aliyun-traffic-bot; fi
userdel aliyunmon 2>/dev/null || true
groupdel aliyunmon 2>/dev/null || true
echo "卸载完成。"
