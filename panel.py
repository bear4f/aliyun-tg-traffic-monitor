#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive terminal panel for the Aliyun Telegram Traffic Monitor.

Everything that needs an AccessKey Secret lives here rather than in Telegram,
so credentials never travel through a chat. Values typed here are validated
against the real Aliyun API before they are written to disk.
"""

from __future__ import annotations

import argparse
import getpass
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import (
    CONFIG_PATH,
    GIB,
    PROVIDERS,
    SAFE_ACTIONS,
    SCOPES,
    SERVICE_NAME,
    STATE_PATH,
    VERSION,
    C,
    ConfigError,
    ConfigStore,
    ID_RE,
    StateStore,
    atomic_write_json,
    bad,
    bold,
    burn_forecast,
    default_config,
    dim,
    fmt_gb,
    human_age,
    instance_defaults,
    month_reset_info,
    ok,
    progress_bar,
    read_json,
    severity,
    status_cn,
    validate_instance_live,
    warn,
)
from zoneinfo import ZoneInfo

SCOPE_MENU = {
    "1": "overseas",
    "2": "mainland",
    "3": "all",
    "4": "exact_region",
}


# --------------------------------------------------------------------------
# Config IO (kept lenient here — validation happens explicitly before saving)
# --------------------------------------------------------------------------


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return default_config()
    try:
        data = read_json(CONFIG_PATH)
    except ConfigError as exc:
        raise SystemExit(bad(str(exc)))
    merged = default_config()
    merged["telegram"].update(data.get("telegram") or {})
    merged["monitor"].update(data.get("monitor") or {})
    merged["instances"] = list(data.get("instances") or [])
    return merged


def atomic_save(data: Dict[str, Any]) -> None:
    atomic_write_json(CONFIG_PATH, data, 0o600)
    if os.geteuid() == 0:
        try:
            os.chown(CONFIG_PATH, pwd.getpwnam("aliyunmon").pw_uid, grp.getgrnam("aliyunmon").gr_gid)
        except (KeyError, PermissionError):
            pass


# --------------------------------------------------------------------------
# Prompt helpers
# --------------------------------------------------------------------------


def ask(prompt: str, default: Optional[str] = None, *, secret: bool = False, allow_empty: bool = False) -> str:
    suffix = dim(f" [{default}]") if default not in (None, "") else ""
    while True:
        try:
            raw = getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")
        except EOFError:
            # Ctrl+D or a closed stdin must cancel cleanly, not traceback.
            print(bad("\n输入流已结束，操作取消。"))
            raise KeyboardInterrupt from None
        value = raw.strip()
        if not value and default is not None:
            return str(default)
        if value or allow_empty:
            return value
        print(bad("该项不能为空。"))


def parse_id_list(raw: str) -> List[int]:
    """Parse comma-separated Telegram IDs, tolerating full-width commas and
    stray spaces from mobile keyboards."""
    normalized = raw.replace("，", ",").replace("；", ",").replace(";", ",")
    return list(dict.fromkeys(int(x.strip()) for x in normalized.split(",") if x.strip()))


def ask_bool(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} {dim('[' + suffix + ']')}: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "1", "是", "开", "开启"}:
            return True
        if value in {"n", "no", "0", "否", "关", "关闭"}:
            return False
        print(bad("请输入 y 或 n。"))


def ask_int(prompt: str, default: int, minimum: int, maximum: int) -> int:
    while True:
        try:
            value = int(ask(prompt, str(default)))
        except ValueError:
            print(bad("请输入整数。"))
            continue
        if minimum <= value <= maximum:
            return value
        print(bad(f"请输入 {minimum}-{maximum} 之间的整数。"))


def ask_float(prompt: str, default: float, minimum: float = 0.01) -> float:
    while True:
        try:
            value = float(ask(prompt, f"{default:g}"))
        except ValueError:
            print(bad("请输入数字。"))
            continue
        if value >= minimum:
            return value
        print(bad(f"数值必须不小于 {minimum:g}。"))


def ask_choice(prompt: str, valid: set, default: str) -> str:
    while True:
        value = ask(prompt, default)
        if value in valid:
            return value
        print(bad(f"请输入以下选项之一：{', '.join(sorted(valid))}"))


def rule(title: str = "") -> None:
    width = min(shutil.get_terminal_size((72, 24)).columns, 72)
    if not title:
        print(dim("─" * width))
        return
    pad = max(0, width - len(title) - 3)
    print(C.paint(f"── {title} ", C.CYAN) + dim("─" * pad))


# --------------------------------------------------------------------------
# Service introspection
# --------------------------------------------------------------------------


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, check=False, timeout=15
    )


def service_state() -> str:
    try:
        result = systemctl("is-active", SERVICE_NAME)
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def render_service_line() -> str:
    state = service_state()
    if state == "active":
        return ok("● 运行中")
    if state == "inactive":
        return warn("○ 已停止")
    if state == "failed":
        return bad("✖ 启动失败")
    return dim(f"? {state}")


# --------------------------------------------------------------------------
# Status bar
# --------------------------------------------------------------------------


def paint_by_severity(text: str, level: str) -> str:
    return {"ok": ok, "warn": warn, "crit": bad}[level](text)


def print_status_bar(data: Dict[str, Any]) -> None:
    """Live overview rendered from state.json, which the running service
    refreshes every interval — no API calls, so the menu opens instantly."""
    instances = data.get("instances", [])
    try:
        tz = ZoneInfo(str(data.get("monitor", {}).get("timezone", "Asia/Taipei")))
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    _, days_left = month_reset_info(now)

    rule(f"Aliyun Traffic Monitor {VERSION}")
    print(
        f"  服务 {render_service_line()}    "
        f"账期 {bold(now.strftime('%Y-%m'))} · {bold(str(days_left))} 天后重置    "
        f"机器 {bold(str(len(instances)))} 台"
    )

    if not instances:
        print(dim("  尚未添加任何机器。"))
        rule()
        return

    state = StateStore(STATE_PATH)
    print()
    for inst in instances:
        name = str(inst.get("name", inst.get("id", "?")))[:16]
        threshold = int(inst.get("shutdown_percent", 95))
        raw = state.instance(str(inst.get("id", ""))).get("last_snapshot") or {}

        if not inst.get("enabled", True):
            print(f"  {dim('⏸')} {name:<16} {dim('已停用')}")
            continue
        if not raw.get("checked_at"):
            print(f"  {dim('○')} {name:<16} {dim('尚未查询')}")
            continue
        if raw.get("error"):
            print(f"  {bad('✖')} {name:<16} {bad('查询失败')} {dim(str(raw['error'])[:40])}")
            continue

        percent = float(raw.get("percent", 0))
        level = severity(percent, threshold)
        icon = {"Running": ok("●"), "Stopped": dim("○")}.get(str(raw.get("status")), warn("◐"))
        bar = paint_by_severity(progress_bar(percent, 14), level)
        usage = f"{fmt_gb(raw.get('used_bytes', 0))} / {fmt_gb(raw.get('total_bytes', 0))}"
        age = human_age(time.time() - float(raw.get("checked_at", 0)))
        daily, days = burn_forecast(
            int(raw.get("used_bytes", 0)), int(raw.get("total_bytes", 0)), threshold, now
        )
        pace = ""
        if daily > 0:
            pace = f" · 日均 {daily / GIB:.1f}G"
            if days == 0.0:
                pace += " · " + bad("已达线")
            elif days is not None and days < 1:
                pace += " · " + bad("≈1天内触线")
            elif days is not None:
                mark = warn if days <= 5 else dim
                pace += " · " + mark(f"≈{days:.0f}天触线")
        print(
            f"  {icon} {name:<16} {bar} "
            f"{paint_by_severity(f'{percent:5.1f}%', level)}  "
            f"{usage:<22} {dim('熔断 ' + str(threshold) + '% · ' + age)}{pace}"
        )
    rule()


# --------------------------------------------------------------------------
# Instance configuration with live validation
# --------------------------------------------------------------------------


def choose_provider(default: str = "ecs_cdt") -> str:
    print("  1) ECS + CDT     独立阿里云账号的 CDT 免费流量池")
    print("  2) SWAS 轻量服务器  读取单实例流量包")
    return "ecs_cdt" if ask_choice("选择产品类型", {"1", "2"}, "1" if default == "ecs_cdt" else "2") == "1" else "swas"


def choose_scope(default: str = "overseas") -> str:
    reverse = {v: k for k, v in SCOPE_MENU.items()}
    print(dim("  CDT 免费额度分两个池：非中国内地 200 GB/月，中国内地 20 GB/月。"))
    print(dim("  香港、新加坡、日本、美国等地域都计入「非中国内地」池。"))
    for menu_key, scope in SCOPE_MENU.items():
        print(f"  {menu_key}) {SCOPES[scope]}")
    return SCOPE_MENU[ask_choice("选择流量口径", set(SCOPE_MENU), reverse.get(default, "1"))]


def unique_id(data: Dict[str, Any], current_id: Optional[str] = None) -> str:
    used = {str(x.get("id", "")) for x in data.get("instances", []) if x.get("id") != current_id}
    default = current_id or f"node{len(data.get('instances', [])) + 1}"
    while True:
        key = ask("内部短 ID", default)
        if not ID_RE.fullmatch(key):
            print(bad("ID 仅允许 1-24 位字母、数字、下划线或短横线。"))
        elif key in used:
            print(bad("该 ID 已存在。"))
        else:
            return key


def run_live_validation(item: Dict[str, Any]) -> bool:
    """Probe the real API immediately, so a typo surfaces here rather than as
    a Telegram error notification hours later. Returns True to keep the entry."""
    print()
    print(dim("  正在调用阿里云 API 验证……"))
    result = validate_instance_live(item)
    for message in result.messages:
        print(f"  {ok('✔') if result.okay else warn('•')} {message}")

    if result.okay:
        level = severity(result.percent, int(item.get("shutdown_percent", 95)))
        print(
            f"  {ok('✔')} 当前用量 "
            f"{paint_by_severity(f'{result.percent:.1f}%', level)} "
            f"({fmt_gb(result.used_bytes)} / {fmt_gb(result.total_bytes)})"
        )
        print(ok("  验证通过。"))
        return True

    print(f"  {bad('✖')} {result.error[:400]}")
    print()
    print(warn("  验证失败。常见原因："))
    print(dim("    · AccessKey ID / Secret 抄错或已停用"))
    print(dim("    · Region 与 Instance ID 不匹配（实例在别的地域）"))
    print(dim("    · RAM 用户缺少 ram-policy-ecs-cdt.json 里的权限"))
    print(dim("    · 该账号尚未开通 CDT"))
    print()
    return ask_bool("仍然保存这条配置", False)


def configure_instance(data: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    item = deepcopy(existing or {})
    is_edit = existing is not None
    rule("修改机器" if is_edit else "添加机器")

    item["id"] = unique_id(data, str(item.get("id")) if is_edit else None)
    item["name"] = ask("显示名称", str(item.get("name") or item["id"]))
    item["provider"] = choose_provider(str(item.get("provider", "ecs_cdt")))
    item["region"] = ask("Region ID（如 cn-hongkong / ap-southeast-1）", str(item.get("region") or "") or None)
    item["instance_id"] = ask("Instance ID", str(item.get("instance_id") or "") or None)
    item["access_key_id"] = ask("RAM AccessKey ID", str(item.get("access_key_id") or "") or None)

    if str(item.get("access_key_secret") or ""):
        entered = ask("RAM AccessKey Secret（留空保留现有值）", "", secret=True, allow_empty=True)
        if entered:
            item["access_key_secret"] = entered
    else:
        item["access_key_secret"] = ask("RAM AccessKey Secret", secret=True)

    if item["provider"] == "ecs_cdt":
        item["quota_gb"] = ask_float("该账号 CDT 月度额度（GB）", float(item.get("quota_gb", 200)))
        item["traffic_scope"] = choose_scope(str(item.get("traffic_scope", "overseas")))
        if item.get("stopped_mode") == "StopCharging":
            print(warn("  旧配置中的 StopCharging 已被移除：节省停机会回收固定公网 IP。"))
        print(dim("  关机固定使用 KeepCharging：保留公网 IP 与所有资源，关机期间实例照常计费。"))
        item["stopped_mode"] = "KeepCharging"
    else:
        for field in ("quota_gb", "traffic_scope", "stopped_mode"):
            item.pop(field, None)

    if item["provider"] == "ecs_cdt":
        quota_bytes = float(item["quota_gb"]) * GIB
        print()
        print(dim(f"  额度按 1 GB = 1024³ 字节换算，{float(item['quota_gb']):g} GB = {quota_bytes / 1e9:.1f} 十进制 GB。"))
        print(dim("  若阿里云的免费额度是十进制口径，95% 会落在免费线之外；90% 更稳妥。"))
    item["shutdown_percent"] = ask_int("达到多少百分比自动关机", int(item.get("shutdown_percent", 95)), 1, 100)
    if item["provider"] == "ecs_cdt":
        actual = float(item["quota_gb"]) * GIB * item["shutdown_percent"] / 100 / 1e9
        note = f"  关机线约等于 {actual:.1f} 十进制 GB。"
        print(warn(note + " 已超过 200 GB 免费额度，可能产生费用。") if actual > 200 else dim(note))
    item["enabled"] = ask_bool("启用该机器监控", bool(item.get("enabled", True)))
    item["auto_shutdown"] = ask_bool("启用自动关机熔断", bool(item.get("auto_shutdown", True)))
    item["auto_start_next_month"] = ask_bool("新账期确认流量重置后自动开机", bool(item.get("auto_start_next_month", False)))
    item["allow_manual_control"] = ask_bool("允许 Telegram 手动开机/关机/重启", bool(item.get("allow_manual_control", True)))
    instance_defaults(item)

    return item if run_live_validation(item) else None


# --------------------------------------------------------------------------
# Telegram / monitor configuration
# --------------------------------------------------------------------------


def verify_bot_token(token: str) -> Optional[str]:
    """Return the bot's @username, or None when the token is rejected."""
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=15
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok"):
            return payload.get("result", {}).get("username")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def fetch_updates_user_ids(token: str) -> List[tuple]:
    """Return [(user_id, display_name)] of everyone who has messaged the bot.

    Used during first-run setup, when the bot service is not running yet and
    /id therefore cannot answer — the wizard reads pending updates directly.
    """
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=100", timeout=15
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return []
    if not payload.get("ok"):
        return []
    seen: Dict[int, str] = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        sender = message.get("from") or {}
        uid = sender.get("id")
        if not isinstance(uid, int):
            continue
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x)
        if sender.get("username"):
            name = (name + f" @{sender['username']}").strip()
        seen[uid] = name or str(uid)
    return list(seen.items())


def autodetect_admin_ids(token: str, username: str) -> str:
    """Interactive loop: user messages the bot, we read the IDs back.
    Returns a comma-joined ID string, or '' when detection was skipped."""
    print()
    print(warn("  Bot 服务此时尚未运行，向它发送 /id 不会有任何回复。"))
    print(f"  请打开 Telegram，向 {bold('@' + username)} 随便发送一条消息（例如 hello）。")
    while True:
        ask("发送完成后按回车，自动读取你的 User ID", "", allow_empty=True)
        detected = fetch_updates_user_ids(token)
        if detected:
            print(ok("  检测到以下用户："))
            for uid, display in detected:
                print(f"    · {bold(str(uid))}  {display}")
            return ",".join(str(uid) for uid, _ in detected)
        print(warn("  尚未收到任何消息。请确认发送对象是这个 Bot，稍等两秒再试。"))
        if not ask_bool("再试一次自动读取", True):
            print(dim("  也可以向 @userinfobot 发送任意消息查到自己的数字 ID，再手动输入。"))
            return ""


def configure_telegram(data: Dict[str, Any], first_run: bool = False) -> None:
    tg = data.setdefault("telegram", {})
    rule("Telegram 配置")
    if str(tg.get("bot_token", "")) and not first_run:
        entered = ask("Bot Token（留空保留现有值）", "", secret=True, allow_empty=True)
        if entered:
            tg["bot_token"] = entered
    else:
        tg["bot_token"] = ask("Telegram Bot Token", secret=True)

    username = verify_bot_token(str(tg["bot_token"]))
    if username:
        print(f"  {ok('✔')} Token 有效，Bot 为 @{username}")
    else:
        print(f"  {warn('•')} 无法验证 Token（可能是网络不通或 Token 有误）")

    existing = ",".join(str(x) for x in tg.get("admin_user_ids", []) if str(x).strip())
    if not existing and username:
        existing = autodetect_admin_ids(str(tg["bot_token"]), username)
    elif existing:
        print(dim("  服务运行期间，向 Bot 发送 /id 可查看 User ID。"))
    while True:
        raw = ask("管理员 Telegram User ID（多个用逗号分隔，回车确认）", existing or None)
        try:
            admins = parse_id_list(raw)
            if not admins:
                raise ValueError
            tg["admin_user_ids"] = admins
            break
        except ValueError:
            print(bad("User ID 必须是数字，多个 ID 用逗号分隔（如 123456789）。"))

    while True:
        try:
            tg["notify_chat_id"] = int(ask("通知 Chat ID", str(tg.get("notify_chat_id") or tg["admin_user_ids"][0])))
            break
        except ValueError:
            print(bad("Chat ID 必须是整数。"))


def configure_monitor(data: Dict[str, Any]) -> None:
    monitor = data.setdefault("monitor", {})
    rule("全局监控设置")
    monitor["interval_seconds"] = ask_int("监控间隔（秒）", int(monitor.get("interval_seconds", 300)), 60, 86400)
    while True:
        current = ",".join(str(x) for x in monitor.get("warning_percentages", [80, 90, 95]))
        try:
            levels = sorted({int(x.strip()) for x in ask("分级提醒线（逗号分隔）", current).split(",") if x.strip()})
            if not levels or any(x < 1 or x > 99 for x in levels):
                raise ValueError
            monitor["warning_percentages"] = levels
            break
        except ValueError:
            print(bad("提醒线必须是 1-99 的逗号分隔整数。"))
    daily = ask("每日汇总时间 HH:MM（输入 off 关闭）", str(monitor.get("daily_report_time", "09:00")))
    monitor["daily_report_time"] = "" if daily.lower() in {"off", "none", "关闭"} else daily
    while True:
        tz_value = ask("时区", str(monitor.get("timezone", "Asia/Taipei")))
        try:
            ZoneInfo(tz_value)
            monitor["timezone"] = tz_value
            break
        except Exception:
            print(bad("无效时区，例如 Asia/Taipei、Asia/Shanghai、UTC。"))
    monitor["error_notify_cooldown_seconds"] = ask_int(
        "API 错误通知冷却（秒）", int(monitor.get("error_notify_cooldown_seconds", 3600)), 60, 604800
    )
    monitor["resume_below_percent"] = ask_int(
        "新账期自动开机确认线（低于该百分比才开机）", int(monitor.get("resume_below_percent", 10)), 0, 50
    )
    monitor["max_concurrency"] = ask_int("并发查询机器数", int(monitor.get("max_concurrency", 5)), 1, 20)
    monitor["telegram_page_size"] = ask_int("Telegram 每页机器数", int(monitor.get("telegram_page_size", 6)), 3, 15)


# --------------------------------------------------------------------------
# Listing, validation, doctor
# --------------------------------------------------------------------------


def list_instances(data: Dict[str, Any]) -> None:
    instances = data.get("instances", [])
    rule("已配置机器")
    if not instances:
        print(dim("  尚未添加机器。"))
        return
    for index, inst in enumerate(instances, 1):
        provider = PROVIDERS.get(str(inst.get("provider")), "?")
        quota = f"{float(inst.get('quota_gb', 0)):g}GB" if inst.get("provider") == "ecs_cdt" else "API 自动读取"
        flags = [
            ok("启用") if inst.get("enabled", True) else dim("停用"),
            ok("熔断") if inst.get("auto_shutdown", True) else warn("仅监测"),
        ]
        print(
            f"  {index:>2}. {bold(str(inst.get('name')))} {dim('[' + str(inst.get('id')) + ']')}\n"
            f"      {provider} · {inst.get('region')} · {inst.get('instance_id')}\n"
            f"      额度 {quota} · 阈值 {inst.get('shutdown_percent', 95)}% · {' · '.join(flags)}"
        )


def select_instance_index(data: Dict[str, Any], prompt: str) -> Optional[int]:
    instances = data.get("instances", [])
    if not instances:
        print(dim("尚未添加机器。"))
        return None
    list_instances(data)
    while True:
        raw = ask(prompt, allow_empty=True)
        if not raw:
            return None
        try:
            index = int(raw) - 1
        except ValueError:
            print(bad("请输入序号。"))
            continue
        if 0 <= index < len(instances):
            return index
        print(bad("序号超出范围。"))


def validate_config(data: Dict[str, Any]) -> List[str]:
    """Structural validation only. Written so the user can be shown every
    problem at once rather than one exception at a time."""
    errors: List[str] = []
    tg = data.get("telegram", {})
    if not str(tg.get("bot_token", "")).strip():
        errors.append("未配置 Telegram Bot Token")
    if not tg.get("admin_user_ids"):
        errors.append("至少需要一个 Telegram 管理员 User ID")
    if not data.get("instances"):
        errors.append("至少需要添加一台机器")
    seen: set = set()
    for index, inst in enumerate(data.get("instances", []), 1):
        prefix = f"机器 {index}"
        key = str(inst.get("id", ""))
        if not ID_RE.fullmatch(key):
            errors.append(f"{prefix}: 内部 ID 无效")
        elif key in seen:
            errors.append(f"{prefix}: 内部 ID 重复 {key}")
        seen.add(key)
        for field in ("name", "region", "instance_id", "access_key_id", "access_key_secret"):
            if not str(inst.get(field, "")).strip():
                errors.append(f"{prefix}: 缺少 {field}")
        if inst.get("provider") == "ecs_cdt":
            try:
                if float(inst.get("quota_gb", 0) or 0) <= 0:
                    errors.append(f"{prefix}: CDT 额度必须大于 0")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: CDT 额度必须是数字")
    return errors


def doctor(data: Dict[str, Any]) -> int:
    """End-to-end health check across config, service, Telegram and Aliyun."""
    problems = 0
    rule("配置结构")
    errors = validate_config(data)
    if errors:
        problems += len(errors)
        for error in errors:
            print(f"  {bad('✖')} {error}")
    else:
        print(f"  {ok('✔')} 配置结构完整，共 {len(data.get('instances', []))} 台机器")

    rule("文件权限")
    for path in (CONFIG_PATH, STATE_PATH):
        if not path.exists():
            print(f"  {dim('○')} {path} 不存在")
            continue
        mode = path.stat().st_mode & 0o777
        if mode == 0o600:
            print(f"  {ok('✔')} {path} 权限 600")
        else:
            problems += 1
            print(f"  {warn('•')} {path} 权限 {oct(mode)}，建议 600（菜单里选『修复权限并重启』）")

    rule("systemd 服务")
    state = service_state()
    if state == "active":
        print(f"  {ok('✔')} {SERVICE_NAME} 运行中")
    else:
        problems += 1
        print(f"  {bad('✖')} {SERVICE_NAME} 状态为 {state}")
        print(dim(f"     journalctl -u {SERVICE_NAME} -n 50 --no-pager"))

    rule("时区与账期")
    try:
        tz = ZoneInfo(str(data.get("monitor", {}).get("timezone", "Asia/Taipei")))
        now = datetime.now(tz)
        _, days_left = month_reset_info(now)
        print(f"  {ok('✔')} 当前 {now:%Y-%m-%d %H:%M} · 账期 {now:%Y-%m} · {days_left} 天后重置")
    except Exception as exc:
        problems += 1
        print(f"  {bad('✖')} 时区无效：{exc}")

    rule("Telegram")
    token = str(data.get("telegram", {}).get("bot_token", ""))
    if not token:
        problems += 1
        print(f"  {bad('✖')} 未配置 Bot Token")
    else:
        username = verify_bot_token(token)
        if username:
            print(f"  {ok('✔')} Token 有效 · @{username}")
        else:
            problems += 1
            print(f"  {bad('✖')} Token 无效或无法连接 api.telegram.org")

    rule("实例 / EIP 安全")
    print(f"  {ok('✔')} 本工具只调用以下 API，无删除实例、释放/解绑 EIP 的能力：")
    for action in SAFE_ACTIONS:
        print(dim(f"      · {action}"))
    print(f"  {ok('✔')} 关机固定 KeepCharging，保留公网 IP 与全部资源")
    legacy = [
        str(i.get("name", i.get("id")))
        for i in data.get("instances", [])
        if str(i.get("stopped_mode", "")) == "StopCharging"
    ]
    if legacy:
        print(f"  {warn('•')} 旧配置含 StopCharging（{', '.join(legacy)}），运行时已强制改为 KeepCharging，保存配置后消除本提示")
    print(dim("      建议在 RAM 用户上仅附加 ram-policy-ecs-cdt.json（含显式 Deny 删除/释放 EIP）"))

    rule("阿里云 API")
    for inst in data.get("instances", []):
        name = str(inst.get("name", inst.get("id")))
        if not inst.get("enabled", True):
            print(f"  {dim('○')} {name} 已停用，跳过")
            continue
        result = validate_instance_live(instance_defaults(dict(inst)))
        if result.okay:
            level = severity(result.percent, int(inst.get("shutdown_percent", 95)))
            print(
                f"  {ok('✔')} {name} · {status_cn(result.status)} · "
                f"{paint_by_severity(f'{result.percent:.1f}%', level)} "
                f"({fmt_gb(result.used_bytes)} / {fmt_gb(result.total_bytes)})"
            )
        else:
            problems += 1
            print(f"  {bad('✖')} {name} · {result.error[:200]}")

    rule()
    if problems:
        print(bad(f"发现 {problems} 处问题，请按上面的提示处理。"))
    else:
        print(ok("全部检查通过。"))
    return 1 if problems else 0


# --------------------------------------------------------------------------
# Menus
# --------------------------------------------------------------------------


def initial_wizard(data: Dict[str, Any]) -> Dict[str, Any]:
    rule("首次配置向导")
    print(dim("  机器数量不设上限，逐台添加即可。"))
    print(dim("  建议把本程序部署在不会被自动关机的第三台管理服务器上。"))
    configure_telegram(data, first_run=True)
    configure_monitor(data)
    while True:
        item = configure_instance(data)
        if item:
            data["instances"].append(item)
            print(ok(f"  已添加 {item['name']}。"))
        if not ask_bool("继续添加下一台机器", not data["instances"]):
            break
    return data


def instance_menu(data: Dict[str, Any]) -> bool:
    dirty = False
    while True:
        rule("机器管理")
        print("  1) 查看机器列表")
        print("  2) 添加机器")
        print("  3) 修改机器（额度、阈值、AccessKey、地域等）")
        print("  4) 删除机器")
        print("  5) 启用 / 停用机器")
        print("  0) 返回上级")
        choice = ask_choice("请选择", set("012345"), "1")

        if choice == "0":
            return dirty
        if choice == "1":
            list_instances(data)
        elif choice == "2":
            item = configure_instance(data)
            if item:
                data["instances"].append(item)
                dirty = True
                print(ok(f"  已添加 {item['name']}。"))
        elif choice == "3":
            index = select_instance_index(data, "输入要修改的机器序号（回车取消）")
            if index is not None:
                item = configure_instance(data, data["instances"][index])
                if item:
                    data["instances"][index] = item
                    dirty = True
                    print(ok("  已更新。"))
        elif choice == "4":
            index = select_instance_index(data, "输入要删除的机器序号（回车取消）")
            if index is not None:
                target = data["instances"][index]
                if ask_bool(f"确认删除 {target.get('name')} [{target.get('id')}]", False):
                    del data["instances"][index]
                    dirty = True
                    print(ok("  已删除。"))
        elif choice == "5":
            index = select_instance_index(data, "输入机器序号（回车取消）")
            if index is not None:
                inst = data["instances"][index]
                inst["enabled"] = not bool(inst.get("enabled", True))
                dirty = True
                print(ok(f"  {inst.get('name')} 已{'启用' if inst['enabled'] else '停用'}。"))


def management_menu(data: Dict[str, Any]) -> bool:
    dirty = False
    while True:
        print()
        print_status_bar(data)
        print("  1) 机器管理（增删改、启停）")
        print("  2) Telegram 配置")
        print("  3) 全局监控设置")
        print("  4) 一键自检 / 诊断")
        print("  5) 保存并退出")
        print("  0) 放弃修改并退出")
        choice = ask_choice("请选择", set("012345"), "1")

        if choice == "1":
            dirty = instance_menu(data) or dirty
        elif choice == "2":
            configure_telegram(data)
            dirty = True
        elif choice == "3":
            configure_monitor(data)
            dirty = True
        elif choice == "4":
            if dirty:
                print(warn("  提示：诊断使用的是当前内存中的配置，尚未保存。"))
            doctor(data)
            input(dim("按回车返回菜单……"))
        elif choice == "5":
            errors = validate_config(data)
            if errors:
                print(bad("\n配置尚不能保存："))
                for error in errors:
                    print(f"  - {error}")
                continue
            atomic_save(data)
            print(ok(f"\n配置已保存：{CONFIG_PATH}"))
            return True
        elif choice == "0":
            if dirty and not ask_bool("存在未保存修改，确认放弃", False):
                continue
            print(dim("未保存任何修改。"))
            return False


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="阿里云 Telegram 流量监控配置管理")
    parser.add_argument("--init", action="store_true", help="强制进入首次配置向导")
    parser.add_argument("--check", action="store_true", help="仅做结构校验，不联网")
    parser.add_argument("--status", action="store_true", help="打印实时状态栏")
    parser.add_argument("--doctor", action="store_true", help="联网自检：配置、服务、Telegram、阿里云")
    args = parser.parse_args()

    data = load_config()

    if args.status:
        print_status_bar(data)
        return
    if args.doctor:
        raise SystemExit(doctor(data))
    if args.check:
        errors = validate_config(data)
        if errors:
            print(bad("配置检查失败："))
            for error in errors:
                print(f"  - {error}")
            raise SystemExit(1)
        print(ok(f"配置检查通过，共 {len(data.get('instances', []))} 台机器。"))
        return

    has_config = bool(str(data.get("telegram", {}).get("bot_token", "")).strip() and data.get("instances"))
    if args.init or not has_config:
        data = initial_wizard(default_config())
        errors = validate_config(data)
        if errors:
            print(bad("配置错误："))
            for error in errors:
                print(f"  - {error}")
            raise SystemExit(1)
        atomic_save(data)
        print(ok(f"\n配置已保存：{CONFIG_PATH}"))
        return

    management_menu(data)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(dim("\n操作已取消。"))
        sys.exit(130)
