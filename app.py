#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-only Aliyun traffic monitor for ECS/CDT and SWAS.

The Telegram side is a single self-refreshing panel: every action edits the
same message in place rather than pushing a new bubble, so the chat stays a
control surface instead of a log.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from common import (
    APP_DIR,
    CONFIG_PATH,
    GIB,
    LOG_PATH,
    PROVIDERS,
    STATE_PATH,
    VERSION,
    AliyunClient,
    ConfigError,
    ConfigStore,
    StateStore,
    UsageSnapshot,
    burn_forecast,
    fmt_gb,
    human_age,
    month_reset_info,
    progress_bar,
    status_cn,
    status_icon,
)

ACTION_CN = {"start": "开机", "stop": "关机", "reboot": "重启"}
HOME = "🏠 返回主面板"


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class MonitorService:
    def __init__(self, config: ConfigStore, state: StateStore):
        self.config = config
        self.state = state
        self.lock = asyncio.Lock()
        self.log = logging.getLogger("monitor")
        self.last_snapshots: Dict[str, UsageSnapshot] = {}
        self.hydrate()

    # -- lifecycle ---------------------------------------------------------

    def hydrate(self) -> None:
        """Restore the previous run's readings so a restarted bot shows real
        numbers immediately instead of '尚未查询'."""
        for inst in self.config.instances:
            raw = self.state.instance(inst["id"]).get("last_snapshot") or {}
            snap = UsageSnapshot.from_state(inst["id"], inst, raw)
            if snap:
                self.last_snapshots[inst["id"]] = snap

    @property
    def tz(self):
        return self.config.tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def enabled_instances(self) -> List[Dict[str, Any]]:
        return [x for x in self.config.instances if x.get("enabled", True)]

    def is_authorized(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in self.config.telegram["admin_user_ids"])

    async def reject(self, update: Update) -> None:
        text = "⛔ 无权限。请把你的 Telegram 用户 ID 加入管理员列表（/id 可查看）。"
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text)

    # -- API wrappers ------------------------------------------------------

    async def api_snapshot(self, inst: Dict[str, Any]) -> UsageSnapshot:
        try:
            snap = await asyncio.to_thread(AliyunClient(inst).get_snapshot)
        except Exception as exc:
            snap = UsageSnapshot(
                instance_key=inst["id"],
                name=inst["name"],
                provider=inst["provider"],
                status="Unknown",
                used_bytes=0,
                total_bytes=0,
                remaining_bytes=0,
                checked_at=time.time(),
                error=str(exc),
            )
        self.last_snapshots[inst["id"]] = snap
        self.state.instance(inst["id"])["last_snapshot"] = snap.to_state()
        self.state.save()
        return snap

    async def api_control(self, inst: Dict[str, Any], action: str) -> None:
        await asyncio.to_thread(AliyunClient(inst).control, action)

    # -- monitoring --------------------------------------------------------

    async def send_notify(self, app: Application, text: str) -> None:
        try:
            await app.bot.send_message(
                chat_id=self.config.telegram["notify_chat_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            self.log.error("Telegram 通知失败: %s", exc)

    async def _handle_month_change(self, app: Application, current_month: str) -> None:
        old_month = self.state.data.get("month", "")
        if old_month == current_month:
            return
        for inst in self.config.instances:
            s = self.state.instance(inst["id"])
            previous_shutdown = bool(s.get("shutdown_triggered"))
            s["warned_levels"] = []
            s["pending_auto_start"] = previous_shutdown and bool(inst.get("auto_start_next_month"))
            s["shutdown_triggered"] = False
            s["last_error_notify"] = 0
        self.state.data["month"] = current_month
        self.state.save()
        if old_month:
            self.log.info("账期切换: %s -> %s", old_month, current_month)
            await self.send_notify(
                app,
                f"🗓️ <b>新账期已开始</b>\n{html.escape(old_month)} → {html.escape(current_month)}\n"
                f"提醒线与熔断标记已重置。",
            )

    async def check_once(self, app: Application, enforce: bool = True) -> List[UsageSnapshot]:
        async with self.lock:
            await self._handle_month_change(app, self.now().strftime("%Y-%m"))
            enabled = self.enabled_instances()
            semaphore = asyncio.Semaphore(int(self.config.monitor["max_concurrency"]))

            async def fetch(inst: Dict[str, Any]) -> Tuple[Dict[str, Any], UsageSnapshot]:
                async with semaphore:
                    return inst, await self.api_snapshot(inst)

            results = await asyncio.gather(*(fetch(inst) for inst in enabled))
            snapshots: List[UsageSnapshot] = []

            for inst, snap in results:
                snapshots.append(snap)
                st = self.state.instance(inst["id"])

                if snap.error:
                    cooldown = int(self.config.monitor["error_notify_cooldown_seconds"])
                    if time.time() - float(st.get("last_error_notify", 0)) >= cooldown:
                        await self.send_notify(
                            app,
                            f"⚠️ <b>{html.escape(inst['name'])} 查询失败</b>\n"
                            f"<code>{html.escape(snap.error[:800])}</code>\n\n"
                            f"查询失败期间不会执行自动关机。",
                        )
                        st["last_error_notify"] = time.time()
                    self.state.save()
                    continue

                if st.get("pending_auto_start"):
                    resume_below = float(self.config.monitor["resume_below_percent"])
                    if snap.percent <= resume_below and snap.status == "Stopped":
                        try:
                            await self.api_control(inst, "start")
                            st["pending_auto_start"] = False
                            await self.send_notify(
                                app,
                                f"▶️ <b>{html.escape(inst['name'])}</b> 新账期流量已重置，已自动开机。",
                            )
                        except Exception as exc:
                            self.log.error("%s 新账期自动开机失败: %s", inst["name"], exc)
                    elif snap.status == "Running":
                        st["pending_auto_start"] = False

                warned = {int(x) for x in st.get("warned_levels", [])}
                for level in self.config.monitor["warning_percentages"]:
                    if snap.percent >= level and level not in warned:
                        await self.send_notify(app, self.format_alert(inst, snap, f"达到 {level}% 提醒线"))
                        warned.add(level)
                st["warned_levels"] = sorted(warned)

                shutdown_percent = int(inst.get("shutdown_percent", 95))
                if (
                    enforce
                    and inst.get("auto_shutdown", True)
                    and snap.percent >= shutdown_percent
                    and not st.get("shutdown_triggered", False)
                ):
                    if snap.status == "Running":
                        try:
                            await self.api_control(inst, "stop")
                            st["shutdown_triggered"] = True
                            await self.send_notify(app, self.format_alert(inst, snap, "已触发自动关机 🛑"))
                        except Exception as exc:
                            await self.send_notify(
                                app, self.format_alert(inst, snap, f"自动关机失败：{exc}")
                            )
                    elif snap.status == "Stopped":
                        st["shutdown_triggered"] = True
                        await self.send_notify(
                            app, self.format_alert(inst, snap, "流量已超阈值，实例当前已关机")
                        )
                    else:
                        self.log.warning(
                            "%s 已超阈值但状态为 %s，暂不发送停机", inst["name"], snap.status
                        )

                self.state.save()
            return snapshots

    async def monitor_loop(self, app: Application) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                snapshots = await self.check_once(app, enforce=True)
                await self.maybe_daily_report(app, snapshots)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("监控循环异常")
            await asyncio.sleep(int(self.config.monitor["interval_seconds"]))

    async def maybe_daily_report(self, app: Application, snapshots: List[UsageSnapshot]) -> None:
        daily_time = str(self.config.monitor.get("daily_report_time", "")).strip()
        if not daily_time:
            return
        today = self.now().strftime("%Y-%m-%d")
        if self.state.data.get("last_daily_report") == today:
            return
        try:
            target_hour, target_minute = [int(x) for x in daily_time.split(":", 1)]
        except Exception:
            return
        now = self.now()
        if (now.hour, now.minute) < (target_hour, target_minute):
            return
        total_pages = self.page_instances(0)[1]
        for page in range(total_pages):
            await self.send_notify(app, self.home_text(page, title="📅 每日流量汇总"))
        self.state.data["last_daily_report"] = today
        self.state.save()

    # -- text rendering ----------------------------------------------------

    def format_alert(self, inst: Dict[str, Any], snap: UsageSnapshot, action: str) -> str:
        return (
            f"🚨 <b>{html.escape(inst['name'])}</b>\n"
            f"<code>{progress_bar(snap.percent)}</code> {snap.percent:.1f}%\n"
            f"{fmt_gb(snap.used_bytes)} / {fmt_gb(snap.total_bytes)} · "
            f"剩余 {fmt_gb(snap.remaining_bytes)}\n"
            f"状态：{status_icon(snap.status)} {html.escape(status_cn(snap.status))}\n"
            f"动作：<b>{html.escape(action)}</b>\n"
            f"<i>{html.escape(snap.scope_note)}</i>"
        )

    def pace_line(self, inst: Dict[str, Any], snap: UsageSnapshot) -> str:
        """One compact burn-rate line, empty when no meaningful estimate."""
        daily, days = burn_forecast(
            snap.used_bytes, snap.total_bytes, int(inst.get("shutdown_percent", 95)), self.now()
        )
        if daily <= 0:
            return ""
        if days is None:
            return f"📈 日均 {fmt_gb(daily, 1)} · 本账期无触线风险"
        if days == 0.0:
            return f"📈 日均 {fmt_gb(daily, 1)} · 已达熔断线"
        if days < 1:
            return f"📈 日均 {fmt_gb(daily, 1)} · ⚠️ 不足 1 天触线"
        return f"📈 日均 {fmt_gb(daily, 1)} · 约 {days:.0f} 天后触线"

    def instance_block(self, inst: Dict[str, Any]) -> str:
        """One expanded machine card for the home panel."""
        name = html.escape(inst["name"])
        snap = self.last_snapshots.get(inst["id"])
        if snap is None:
            return f"⚪ <b>{name}</b>\n<i>尚未查询，点击「刷新全部」</i>"
        if snap.error:
            return (
                f"⚠️ <b>{name}</b> · 查询失败\n"
                f"<code>{html.escape(snap.error[:160])}</code>"
            )
        threshold = int(inst.get("shutdown_percent", 95))
        shield = "开" if inst.get("auto_shutdown", True) else "关"
        if self.state.instance(inst["id"]).get("shutdown_triggered"):
            shield += "（本账期已触发🛑）"
        resume = "开" if inst.get("auto_start_next_month") else "关"
        line = (
            f"{status_icon(snap.status)} <b>{name}</b> · {html.escape(status_cn(snap.status))}\n"
            f"<code>{progress_bar(snap.percent)}</code> <b>{snap.percent:.1f}%</b>\n"
            f"{fmt_gb(snap.used_bytes)} / {fmt_gb(snap.total_bytes)} · 剩余 {fmt_gb(snap.remaining_bytes)}\n"
            f"🛡️ 熔断 {threshold}% {shield} · 🗓️ 下月开机 {resume}"
        )
        pace = self.pace_line(inst, snap)
        if pace:
            line += f"\n{pace}"
        if snap.overflow_bytes > 0:
            line += f"\n❗ 已超额 {fmt_gb(snap.overflow_bytes)}"
        return line

    def page_instances(self, page: int) -> Tuple[int, int, List[Dict[str, Any]]]:
        enabled = self.enabled_instances()
        page_size = int(self.config.monitor["telegram_page_size"])
        total_pages = max(1, (len(enabled) + page_size - 1) // page_size)
        page = min(max(0, page), total_pages - 1)
        start = page * page_size
        return page, total_pages, enabled[start : start + page_size]

    def home_text(self, page: int = 0, title: str = "📊 阿里云流量监控") -> str:
        page, total_pages, items = self.page_instances(page)
        now = self.now()
        _, days_left = month_reset_info(now)
        checked = [s.checked_at for s in self.last_snapshots.values() if s.checked_at]
        freshness = human_age(time.time() - max(checked)) if checked else "尚未查询"

        header = [
            f"<b>{title}</b>",
            f"账期 {now.strftime('%Y-%m')} · {days_left} 天后重置 · 更新于 {freshness}",
        ]
        if total_pages > 1:
            header.append(f"第 {page + 1}/{total_pages} 页")

        if not items:
            header.append("\n<i>暂无已启用机器。在管理服务器运行 aliyun-monitor 添加。</i>")
            return "\n".join(header)

        blocks = [self.instance_block(inst) for inst in items]
        disabled = [x for x in self.config.instances if not x.get("enabled", True)]
        footer = ""
        if disabled:
            names = "、".join(html.escape(x["name"]) for x in disabled)
            footer = f"\n\n⏸️ <i>已停用：{names}</i>"
        return "\n".join(header) + "\n\n" + "\n\n".join(blocks) + footer

    def instance_text(self, inst: Dict[str, Any]) -> str:
        name = html.escape(inst["name"])
        snap = self.last_snapshots.get(inst["id"])
        lines = [
            f"🖥️ <b>{name}</b>",
            f"类型：{PROVIDERS[inst['provider']]} · 地域 <code>{html.escape(inst['region'])}</code>",
            f"实例：<code>{html.escape(inst['instance_id'])}</code>",
            "🔒 停机保留公网 IP（KeepCharging，不可更改）",
            "",
        ]
        if snap is None:
            lines.append("<i>尚未查询，点击「🔄 刷新」</i>")
        elif snap.error:
            lines.append(f"⚠️ 查询失败\n<code>{html.escape(snap.error[:500])}</code>")
        else:
            lines += [
                f"状态：{status_icon(snap.status)} <b>{html.escape(status_cn(snap.status))}</b>",
                f"<code>{progress_bar(snap.percent, 16)}</code> <b>{snap.percent:.1f}%</b>",
                f"本月已用：<b>{fmt_gb(snap.used_bytes)}</b> / {fmt_gb(snap.total_bytes)}",
                f"剩余：<b>{fmt_gb(snap.remaining_bytes)}</b>",
            ]
            pace = self.pace_line(inst, snap)
            if pace:
                lines.append(pace)
            if self.state.instance(inst["id"]).get("shutdown_triggered"):
                lines.append("🛑 本账期已触发过自动熔断（新账期自动解除）")
            if snap.overflow_bytes > 0:
                lines.append(f"❗ 已超额：<b>{fmt_gb(snap.overflow_bytes)}</b>")
            lines += [
                "",
                f"<i>{html.escape(snap.scope_note)}</i>",
                f"<i>更新于 {human_age(time.time() - snap.checked_at)}</i>",
            ]
        return "\n".join(lines)

    def global_text(self) -> str:
        m = self.config.monitor
        daily = m.get("daily_report_time") or "已关闭"
        return "\n".join(
            [
                "🛠️ <b>全局设置</b>",
                "",
                f"检查间隔：<b>{m['interval_seconds']} 秒</b>",
                f"分级提醒线：<b>{'/'.join(str(x) for x in m['warning_percentages'])}%</b>",
                f"每日汇总时间：<b>{html.escape(str(daily))}</b>",
                f"时区：<code>{html.escape(str(m['timezone']))}</code>",
                f"新账期开机确认线：<b>≤ {m['resume_below_percent']}%</b>",
                f"并发查询数：<b>{m['max_concurrency']}</b>",
                f"每页机器数：<b>{m['telegram_page_size']}</b>",
                "",
                f"<i>共 {len(self.config.instances)} 台，启用 {len(self.enabled_instances())} 台</i>",
                f"<i>版本 {VERSION}</i>",
            ]
        )

    def admins_text(self, current_user: int) -> str:
        admins = self.config.telegram["admin_user_ids"]
        lines = ["👥 <b>管理员</b>", ""]
        for uid in admins:
            mark = " ← 你" if uid == current_user else ""
            notify = " · 接收通知" if uid == self.config.telegram["notify_chat_id"] else ""
            lines.append(f"• <code>{uid}</code>{mark}{notify}")
        lines += ["", "<i>管理员可以查看面板并控制实例。</i>"]
        return "\n".join(lines)

    # -- keyboards ---------------------------------------------------------

    def home_keyboard(self, page: int = 0) -> InlineKeyboardMarkup:
        page, total_pages, items = self.page_instances(page)
        rows: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton("🔄 刷新全部", callback_data=f"r:a:{page}")]
        ]
        for inst in items:
            key = inst["id"]
            row = [InlineKeyboardButton(f"⚙️ {inst['name']}", callback_data=f"n:i:{key}")]
            snap = self.last_snapshots.get(key)
            if inst.get("allow_manual_control", True) and snap and not snap.error:
                if snap.status == "Running":
                    row.append(InlineKeyboardButton("⏹️ 关机", callback_data=f"c:{key}:stop"))
                elif snap.status == "Stopped":
                    row.append(InlineKeyboardButton("▶️ 开机", callback_data=f"c:{key}:start"))
            rows.append(row)
        if total_pages > 1:
            nav: List[InlineKeyboardButton] = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀️", callback_data=f"n:h:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="nop"))
            if page + 1 < total_pages:
                nav.append(InlineKeyboardButton("▶️", callback_data=f"n:h:{page + 1}"))
            rows.append(nav)
        rows.append(
            [
                InlineKeyboardButton("🛠️ 全局设置", callback_data="n:g"),
                InlineKeyboardButton("ℹ️ 帮助", callback_data="n:?"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def instance_keyboard(self, inst: Dict[str, Any]) -> InlineKeyboardMarkup:
        key = inst["id"]
        threshold = int(inst.get("shutdown_percent", 95))
        rows: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton("🔄 刷新", callback_data=f"r:i:{key}")]
        ]
        if inst.get("allow_manual_control", True):
            rows.append(
                [
                    InlineKeyboardButton("▶️ 开机", callback_data=f"c:{key}:start"),
                    InlineKeyboardButton("⏹️ 关机", callback_data=f"c:{key}:stop"),
                    InlineKeyboardButton("🔁 重启", callback_data=f"c:{key}:reboot"),
                ]
            )
        rows += [
            [
                InlineKeyboardButton(
                    f"🛡️ 自动熔断：{'开' if inst.get('auto_shutdown', True) else '关'}",
                    callback_data=f"t:{key}:auto_shutdown",
                )
            ],
            [
                InlineKeyboardButton("－5%", callback_data=f"p:{key}:-5"),
                InlineKeyboardButton("－1%", callback_data=f"p:{key}:-1"),
                InlineKeyboardButton(f"阈值 {threshold}%", callback_data="nop"),
                InlineKeyboardButton("＋1%", callback_data=f"p:{key}:1"),
                InlineKeyboardButton("＋5%", callback_data=f"p:{key}:5"),
            ],
        ]
        if inst["provider"] == "ecs_cdt":
            rows.append(
                [
                    InlineKeyboardButton(
                        f"📦 月度额度：{float(inst.get('quota_gb', 0)):g} GB",
                        callback_data=f"q:{key}",
                    )
                ]
            )
        rows += [
            [
                InlineKeyboardButton(
                    f"🗓️ 下月自动开机：{'开' if inst.get('auto_start_next_month') else '关'}",
                    callback_data=f"t:{key}:auto_start_next_month",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🎛️ 允许手动控制：{'开' if inst.get('allow_manual_control', True) else '关'}",
                    callback_data=f"t:{key}:allow_manual_control",
                )
            ],
            [
                InlineKeyboardButton(
                    f"{'⏸️ 停用监控' if inst.get('enabled', True) else '▶️ 启用监控'}",
                    callback_data=f"t:{key}:enabled",
                )
            ],
            [InlineKeyboardButton(HOME, callback_data="n:h:0")],
        ]
        return InlineKeyboardMarkup(rows)

    def global_keyboard(self) -> InlineKeyboardMarkup:
        m = self.config.monitor
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("－60s", callback_data="g:interval_seconds:-60"),
                    InlineKeyboardButton(f"间隔 {m['interval_seconds']}s", callback_data="nop"),
                    InlineKeyboardButton("＋60s", callback_data="g:interval_seconds:60"),
                ],
                [
                    InlineKeyboardButton("✏️ 提醒线", callback_data="G:warning_percentages"),
                    InlineKeyboardButton("✏️ 汇总时间", callback_data="G:daily_report_time"),
                ],
                [
                    InlineKeyboardButton("－1%", callback_data="g:resume_below_percent:-1"),
                    InlineKeyboardButton(
                        f"开机确认线 {m['resume_below_percent']}%", callback_data="nop"
                    ),
                    InlineKeyboardButton("＋1%", callback_data="g:resume_below_percent:1"),
                ],
                [InlineKeyboardButton("👥 管理员", callback_data="n:a")],
                [InlineKeyboardButton(HOME, callback_data="n:h:0")],
            ]
        )

    def admins_keyboard(self) -> InlineKeyboardMarkup:
        admins = self.config.telegram["admin_user_ids"]
        rows: List[List[InlineKeyboardButton]] = []
        for uid in admins:
            # The last remaining admin must not be removable, or the panel
            # would lock everyone out until someone edits config.json by hand.
            if len(admins) > 1:
                rows.append([InlineKeyboardButton(f"🗑️ 移除 {uid}", callback_data=f"a:del:{uid}")])
            else:
                rows.append([InlineKeyboardButton(f"🔒 {uid}（唯一管理员）", callback_data="nop")])
        rows += [
            [InlineKeyboardButton("➕ 添加管理员", callback_data="a:add")],
            [
                InlineKeyboardButton("⬅️ 全局设置", callback_data="n:g"),
                InlineKeyboardButton(HOME, callback_data="n:h:0"),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def help_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(HOME, callback_data="n:h:0")]])

    def help_text(self) -> str:
        return "\n".join(
            [
                "ℹ️ <b>使用说明</b>",
                "",
                "<b>命令</b>",
                "/menu 打开面板 · /status 刷新并打开面板",
                "/id 查看自己的 Telegram ID",
                "",
                "<b>计量口径</b>",
                "ECS/CDT 读取的是<b>整个阿里云账号</b>的 CDT 流量池，不是单块网卡。",
                "一个账号只跑一台主力机时最准确。",
                "",
                "<b>熔断规则</b>",
                "• 流量查询失败时<b>绝不</b>自动关机；",
                "• 实例状态不是运行中时不会重复发关机指令；",
                "• 同账期熔断一次后不再重复触发；",
                "• 手动开机时若流量仍超阈值且熔断开启，会被拦截。",
                "",
                "<b>安全边界</b>",
                "• 本工具<b>没有</b>删除实例、释放或解绑 EIP 的能力：",
                "  代码只调用读流量、读状态和开关机 API，",
                "  RAM 策略同时显式 Deny 删除实例与释放 EIP；",
                "• 关机固定 KeepCharging，公网 IP 与资源全部保留；",
                "• 新增/删除机器、修改 AccessKey 只能在管理服务器上运行 "
                "<code>aliyun-monitor</code>，密钥不经过 Telegram。",
            ]
        )


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


async def safe_edit(query, text: str, markup: Optional[InlineKeyboardMarkup]) -> None:
    """Edit in place, tolerating Telegram's 'message is not modified' error
    which fires whenever a refresh produces byte-identical output."""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def render_home(service: MonitorService, query, page: int = 0) -> None:
    await safe_edit(query, service.home_text(page), service.home_keyboard(page))


async def render_instance(service: MonitorService, query, inst: Dict[str, Any]) -> None:
    await safe_edit(query, service.instance_text(inst), service.instance_keyboard(inst))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def command_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    if not service.is_authorized(update):
        await service.reject(update)
        return
    context.user_data.pop("await", None)
    await update.effective_message.reply_text(
        service.home_text(0),
        parse_mode=ParseMode.HTML,
        reply_markup=service.home_keyboard(0),
        disable_web_page_preview=True,
    )


async def command_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    if not service.is_authorized(update):
        await service.reject(update)
        return
    msg = await update.effective_message.reply_text(
        f"🔄 正在查询 {len(service.enabled_instances())} 台机器……"
    )
    await service.check_once(context.application, enforce=True)
    await msg.edit_text(
        service.home_text(0),
        parse_mode=ParseMode.HTML,
        reply_markup=service.home_keyboard(0),
        disable_web_page_preview=True,
    )


async def command_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    chat_id = update.effective_chat.id if update.effective_chat else "Unknown"
    await update.effective_message.reply_text(
        f"User ID: <code>{user_id}</code>\nChat ID: <code>{chat_id}</code>",
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------
# Text input flow (used for values a keyboard cannot express)
# --------------------------------------------------------------------------

PROMPTS = {
    "quota": "请发送新的月度额度，单位 GB，例如 <code>200</code> 或 <code>220</code>",
    "warning_percentages": "请发送分级提醒线，逗号分隔，例如 <code>80,90,95</code>",
    "daily_report_time": "请发送每日汇总时间 <code>HH:MM</code>，发送 <code>off</code> 关闭",
    "admin": "请发送要添加的 Telegram User ID（纯数字，对方可以用 /id 查询）",
}


async def prompt_input(service: MonitorService, query, context, kind: str, key: str = "") -> None:
    context.user_data["await"] = {
        "kind": kind,
        "key": key,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    back = f"n:i:{key}" if kind == "quota" else ("n:a" if kind == "admin" else "n:g")
    await safe_edit(
        query,
        f"✏️ <b>等待输入</b>\n\n{PROMPTS[kind]}\n\n<i>直接在聊天里发送即可，或点下方取消。</i>",
        InlineKeyboardMarkup([[InlineKeyboardButton("✖️ 取消", callback_data=back)]]),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    pending = context.user_data.get("await")
    if not pending or not service.is_authorized(update):
        return
    raw = (update.effective_message.text or "").strip()
    kind, key = pending["kind"], pending["key"]
    error = ""

    try:
        if kind == "quota":
            inst = service.config.get_instance(key)
            if not inst:
                error = "实例已不存在。"
            else:
                value = float(raw)
                if value <= 0:
                    raise ValueError
                inst["quota_gb"] = value
        elif kind == "warning_percentages":
            levels = sorted({int(x.strip()) for x in raw.split(",") if x.strip()})
            if not levels or any(x < 1 or x > 99 for x in levels):
                raise ValueError
            service.config.monitor["warning_percentages"] = levels
        elif kind == "daily_report_time":
            if raw.lower() in {"off", "none", "关闭"}:
                service.config.monitor["daily_report_time"] = ""
            else:
                hour, minute = [int(x) for x in raw.split(":", 1)]
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
                service.config.monitor["daily_report_time"] = f"{hour:02d}:{minute:02d}"
        elif kind == "admin":
            uid = int(raw)
            if uid in service.config.telegram["admin_user_ids"]:
                error = "该用户已经是管理员。"
            else:
                service.config.telegram["admin_user_ids"].append(uid)
    except (ValueError, TypeError):
        error = "格式不正确，请重新发送。"

    if not error:
        try:
            service.config.save()
        except ConfigError as exc:
            error = f"配置校验失败：{exc}"

    # Keep the chat clean: the panel is the only surface that persists.
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    if error:
        # Stay in the waiting state so the value can simply be re-sent.
        try:
            await context.bot.edit_message_text(
                chat_id=pending["chat_id"],
                message_id=pending["message_id"],
                text=f"⚠️ {html.escape(error)}\n\n{PROMPTS[kind]}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✖️ 取消", callback_data="n:h:0")]]
                ),
            )
        except BadRequest:
            pass
        return

    context.user_data.pop("await", None)
    if kind == "quota":
        inst = service.config.get_instance(key)
        text, markup = service.instance_text(inst), service.instance_keyboard(inst)
    elif kind == "admin":
        text = service.admins_text(update.effective_user.id)
        markup = service.admins_keyboard()
    else:
        text, markup = service.global_text(), service.global_keyboard()
    try:
        await context.bot.edit_message_text(
            chat_id=pending["chat_id"],
            message_id=pending["message_id"],
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except BadRequest:
        pass


# --------------------------------------------------------------------------
# Callback router
# --------------------------------------------------------------------------


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    query = update.callback_query
    if not query:
        return
    if not service.is_authorized(update):
        await service.reject(update)
        return

    data = query.data or ""
    parts = data.split(":")
    head = parts[0]

    if data == "nop":
        await query.answer()
        return

    # -- navigation --------------------------------------------------------
    if head == "n":
        await query.answer()
        context.user_data.pop("await", None)
        target = parts[1]
        if target == "h":
            page = int(parts[2]) if len(parts) > 2 else 0
            await render_home(service, query, page)
        elif target == "i":
            inst = service.config.get_instance(parts[2])
            if not inst:
                await render_home(service, query, 0)
                return
            await render_instance(service, query, inst)
        elif target == "g":
            await safe_edit(query, service.global_text(), service.global_keyboard())
        elif target == "a":
            await safe_edit(
                query, service.admins_text(update.effective_user.id), service.admins_keyboard()
            )
        elif target == "?":
            await safe_edit(query, service.help_text(), service.help_keyboard())
        return

    # -- refresh -----------------------------------------------------------
    if head == "r":
        await query.answer("🔄 正在查询……")
        if parts[1] == "a":
            page = int(parts[2]) if len(parts) > 2 else 0
            await service.check_once(context.application, enforce=True)
            await render_home(service, query, page)
        else:
            inst = service.config.get_instance(parts[2])
            if not inst:
                await render_home(service, query, 0)
                return
            await service.api_snapshot(inst)
            await render_instance(service, query, inst)
        return

    # -- global settings ---------------------------------------------------
    if head == "g":
        field, delta = parts[1], int(parts[2])
        bounds = {"interval_seconds": (60, 86400), "resume_below_percent": (0, 50)}
        low, high = bounds[field]
        service.config.monitor[field] = min(high, max(low, int(service.config.monitor[field]) + delta))
        service.config.save()
        await query.answer(f"已设为 {service.config.monitor[field]}")
        await safe_edit(query, service.global_text(), service.global_keyboard())
        return

    if head == "G":
        await query.answer()
        await prompt_input(service, query, context, parts[1])
        return

    # -- admins ------------------------------------------------------------
    if head == "a":
        if parts[1] == "add":
            await query.answer()
            await prompt_input(service, query, context, "admin")
            return
        uid = int(parts[2])
        admins = service.config.telegram["admin_user_ids"]
        if len(admins) <= 1:
            await query.answer("不能移除唯一的管理员。", show_alert=True)
            return
        if uid in admins:
            admins.remove(uid)
            if service.config.telegram["notify_chat_id"] == uid:
                service.config.telegram["notify_chat_id"] = admins[0]
            service.config.save()
        await query.answer("已移除")
        await safe_edit(
            query, service.admins_text(update.effective_user.id), service.admins_keyboard()
        )
        return

    # -- everything below is instance-scoped -------------------------------
    key = parts[1] if len(parts) > 1 else ""
    inst = service.config.get_instance(key)
    if not inst:
        await query.answer("该实例已不存在。", show_alert=True)
        await render_home(service, query, 0)
        return

    if head == "t":
        field = parts[2]
        toggle_defaults = {
            "auto_shutdown": True,
            "auto_start_next_month": False,
            "allow_manual_control": True,
            "enabled": True,
        }
        if field not in toggle_defaults:
            await query.answer()
            return
        inst[field] = not bool(inst.get(field, toggle_defaults[field]))
        # Re-arming the breaker must clear the latch, otherwise it would never
        # fire again this billing cycle.
        if field == "auto_shutdown" and inst[field]:
            service.state.instance(key)["shutdown_triggered"] = False
            service.state.save()
        service.config.save()
        await query.answer(f"已{'开启' if inst[field] else '关闭'}")
        await render_instance(service, query, inst)
        return

    if head == "p":
        delta = int(parts[2])
        inst["shutdown_percent"] = min(100, max(1, int(inst.get("shutdown_percent", 95)) + delta))
        service.config.save()
        await query.answer(f"阈值 {inst['shutdown_percent']}%")
        await render_instance(service, query, inst)
        return

    if head == "q":
        await query.answer()
        await prompt_input(service, query, context, "quota", key)
        return

    if head == "c":
        action = parts[2]
        if action not in ACTION_CN:
            await query.answer()
            return
        await query.answer()
        await safe_edit(
            query,
            f"⚠️ 确认对 <b>{html.escape(inst['name'])}</b> 执行 <b>{ACTION_CN[action]}</b>？",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ 确认{ACTION_CN[action]}", callback_data=f"x:{key}:{action}"
                        )
                    ],
                    [InlineKeyboardButton("✖️ 取消", callback_data=f"n:i:{key}")],
                ]
            ),
        )
        return

    if head == "x":
        action = parts[2]
        if action not in ACTION_CN:
            await query.answer()
            return
        if not inst.get("allow_manual_control", True):
            await query.answer("该实例已禁用手动控制。", show_alert=True)
            return
        if action == "start" and inst.get("auto_shutdown", True):
            snap = await service.api_snapshot(inst)
            if not snap.error and snap.percent >= int(inst.get("shutdown_percent", 95)):
                await query.answer("流量仍高于熔断阈值。", show_alert=True)
                await safe_edit(
                    query,
                    f"🚫 <b>{html.escape(inst['name'])}</b> 当前流量 {snap.percent:.1f}%，"
                    f"仍高于熔断阈值 {inst.get('shutdown_percent', 95)}%。\n\n"
                    f"开机后会立刻被再次自动关机。请先关闭自动熔断、调高阈值，或等待新账期。",
                    service.instance_keyboard(inst),
                )
                return
        await query.answer(f"正在{ACTION_CN[action]}……")
        try:
            await service.api_control(inst, action)
            note = f"✅ 已向阿里云发送 <b>{ACTION_CN[action]}</b> 指令。\n<i>状态需要几十秒才会更新，稍后点刷新。</i>"
        except Exception as exc:
            note = f"❌ 操作失败\n<code>{html.escape(str(exc)[:500])}</code>"
        await safe_edit(query, service.instance_text(inst) + "\n\n" + note, service.instance_keyboard(inst))
        return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.getLogger("monitor").exception("处理更新时异常", exc_info=context.error)


# --------------------------------------------------------------------------


async def post_init(app: Application) -> None:
    service: MonitorService = app.bot_data["service"]
    await app.bot.set_my_commands(
        [
            BotCommand("menu", "打开控制面板"),
            BotCommand("status", "刷新并打开面板"),
            BotCommand("id", "查看 Telegram ID"),
        ]
    )
    app.create_task(service.monitor_loop(app), name="aliyun-monitor-loop")


def main() -> None:
    setup_logging()
    log = logging.getLogger("main")
    try:
        config = ConfigStore(CONFIG_PATH)
        state = StateStore(STATE_PATH)
    except ConfigError as exc:
        log.error("配置错误: %s", exc)
        raise SystemExit(2) from exc

    service = MonitorService(config, state)
    app = ApplicationBuilder().token(config.telegram["bot_token"]).post_init(post_init).build()
    app.bot_data["service"] = service
    app.add_handler(CommandHandler(["start", "menu"], command_menu))
    app.add_handler(CommandHandler("status", command_status))
    app.add_handler(CommandHandler("help", command_menu))
    app.add_handler(CommandHandler("id", command_id))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Aliyun traffic monitor %s starting with %d instance(s)", VERSION, len(config.instances))
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
