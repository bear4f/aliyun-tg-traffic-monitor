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
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    BAR_STYLE_CN,
    BAR_STYLES,
    PANEL_STYLE_CN,
    billing_now,
    burn_forecast,
    fmt_gb,
    month_reset_info,
    progress_bar,
    rolling_rate,
    severity,
    status_cn,
)

ACTION_CN = {"start": "开机", "stop": "关机", "reboot": "重启"}
HOME = "🏠 返回主面板"

# One symbol family per meaning: coloured dots are traffic risk, and nothing
# else. Power state is spelled out in words, so a green dot next to a stopped
# machine can no longer be misread as "it's up".
SEV_ICON = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}
BAR_WIDTH = 10
DETAIL_BAR_WIDTH = 14
# Telegram hard-caps message text at 4096 characters. Panels are built to stay
# well inside that, and the home view drops overflowing cards rather than
# letting the API reject the whole edit.
TG_TEXT_LIMIT = 4096
HOME_TEXT_BUDGET = 3600
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text))


def short_name(name: str, limit: int = 11) -> str:
    """Button labels are truncated by Telegram from the right, which eats the
    percentage — the only part that changes. Trim the name ourselves instead."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


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
        self.instance_locks: Dict[str, asyncio.Lock] = {}
        self.log = logging.getLogger("monitor")
        self.last_snapshots: Dict[str, UsageSnapshot] = {}
        self.started_at = time.time()
        self.hydrate()
        self.seed_daily_report()

    # -- lifecycle ---------------------------------------------------------

    def hydrate(self) -> None:
        """Restore the previous run's readings so a restarted bot shows real
        numbers immediately instead of '尚未查询'."""
        for inst in self.config.instances:
            raw = self.state.instance(inst["id"]).get("last_snapshot") or {}
            snap = UsageSnapshot.from_state(inst["id"], inst, raw)
            if snap:
                self.last_snapshots[inst["id"]] = snap

    def seed_daily_report(self) -> None:
        """On a brand-new install, mark today's summary as already sent when
        its scheduled time is behind us — otherwise the first thing a fresh
        setup does at 23:00 is fire off a '每日汇总' for a day it never watched."""
        if self.state.data.get("last_daily_report"):
            return
        daily_time = str(self.config.monitor.get("daily_report_time", "")).strip()
        if not daily_time:
            return
        try:
            hour, minute = [int(x) for x in daily_time.split(":", 1)]
        except ValueError:
            return
        now = self.now()
        if (now.hour, now.minute) >= (hour, minute):
            self.state.data["last_daily_report"] = now.strftime("%Y-%m-%d")
            self.state.save()

    def instance_lock(self, key: str) -> asyncio.Lock:
        """One in-flight query per machine. Double-tapping 刷新 used to fire two
        concurrent Aliyun round-trips that then raced each other's state writes."""
        return self.instance_locks.setdefault(key, asyncio.Lock())

    def sync_config(self) -> bool:
        """Adopt config.json edits made outside Telegram (terminal panel, hand
        edits) before we read or overwrite it."""
        try:
            if not self.config.reload_if_changed():
                return False
        except ConfigError as exc:
            self.log.warning("config.json 已变更但校验失败，继续沿用内存中的配置: %s", exc)
            return False
        self.hydrate()
        self.log.info("检测到 config.json 变更，已热加载")
        return True

    @property
    def tz(self):
        return self.config.tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    @staticmethod
    def billing_now() -> datetime:
        return billing_now()

    def fmt_time(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d %H:%M")

    def fmt_clock(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%H:%M")

    def fmt_interval(self, seconds: int) -> str:
        seconds = int(seconds)
        if seconds % 3600 == 0:
            return f"{seconds // 3600} 小时"
        if seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    def enabled_instances(self) -> List[Dict[str, Any]]:
        return [x for x in self.config.instances if x.get("enabled", True)]

    def log_event(self, text: str) -> None:
        """Append to the panel-visible audit trail: breaker trips, power
        actions, config changes, query outages. Routine successful checks
        stay out so the log doesn't drown."""
        events = self.state.data.setdefault("events", [])
        events.append({"t": time.time(), "text": text})
        del events[:-50]
        self.state.save()

    def _bump_api_stats(self, ok: bool) -> None:
        stats = self.state.data.setdefault("api_stats", {})
        bucket = datetime.now(self.tz).strftime("%Y%m%d%H")
        pair = stats.setdefault(bucket, [0, 0])
        pair[0 if ok else 1] += 1
        for stale in sorted(stats)[:-24]:
            stats.pop(stale, None)

    def api_stats_24h(self) -> Tuple[int, int]:
        stats = self.state.data.get("api_stats") or {}
        ok = sum(int(v[0]) for v in stats.values())
        fail = sum(int(v[1]) for v in stats.values())
        return ok, fail

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
        async with self.instance_lock(inst["id"]):
            return await self._api_snapshot(inst)

    async def _api_snapshot(self, inst: Dict[str, Any]) -> UsageSnapshot:
        st = self.state.instance(inst["id"])
        try:
            snap = await asyncio.to_thread(AliyunClient(inst).get_snapshot)
        except Exception as exc:
            # A failure must not wipe the last good reading off the panels:
            # record the error stream beside it instead of overwriting it.
            streak = int(st.get("error_streak", 0) or 0) + 1
            st["error_streak"] = streak
            if streak == 1:
                st["error_since"] = time.time()
            st["last_error"] = str(exc)
            st["last_error_at"] = time.time()
            self._bump_api_stats(ok=False)
            self.state.save()
            return UsageSnapshot(
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
        if int(st.get("error_streak", 0) or 0) > 0:
            # Hand the closed error stream to check_once so it can announce
            # the recovery even when this success came from a manual refresh.
            st["recovered"] = {
                "streak": int(st.get("error_streak", 0) or 0),
                "since": float(st.get("error_since", 0) or 0),
                "at": time.time(),
                "notified": bool(st.get("error_notified")),
            }
        st["error_streak"] = 0
        st["error_since"] = 0
        st["last_error"] = ""
        st["last_error_at"] = 0
        st["error_notified"] = False
        st["last_error_notify"] = 0
        # The traffic read succeeded; a failed power-state read is recorded
        # beside it rather than treated as an outage.
        st["status_error"] = snap.status_error
        if not snap.status_error:
            st["unknown_status_notified"] = False
        self._bump_api_stats(ok=True)
        self.last_snapshots[inst["id"]] = snap
        st["last_snapshot"] = snap.to_state()
        # Hourly usage samples power the 24h/7d rolling rates and the
        # month-end forecast; ~200 points ≈ 8 days.
        hist = st.setdefault("usage_history", [])
        if not hist or snap.checked_at - float(hist[-1].get("t", 0)) >= 3600:
            hist.append({"t": snap.checked_at, "u": snap.used_bytes})
            del hist[:-200]
        self.state.save()
        return snap

    async def api_control(self, inst: Dict[str, Any], action: str) -> None:
        await asyncio.to_thread(AliyunClient(inst).control, action)

    # -- monitoring --------------------------------------------------------

    async def send_notify(self, app: Application, text: str):
        try:
            return await app.bot.send_message(
                chat_id=self.config.telegram["notify_chat_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            self.log.error("Telegram 通知失败: %s", exc)
            return None

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
            s["over_threshold_checks"] = 0
            s["last_error_notify"] = 0
        self.state.data["month"] = current_month
        self.state.save()
        if old_month:
            self.log.info("账期切换: %s -> %s", old_month, current_month)
            self.log_event(f"账期切换 {old_month} → {current_month}，提醒线与熔断标记已重置")
            await self.send_notify(
                app,
                f"🗓️ <b>新账期已开始</b>\n{html.escape(old_month)} → {html.escape(current_month)}\n"
                f"提醒线与熔断标记已重置。",
            )

    def failing_ids(self) -> List[str]:
        return [
            inst["id"]
            for inst in self.enabled_instances()
            if int(self.state.instance(inst["id"]).get("error_streak", 0) or 0) > 0
        ]

    async def check_once(
        self, app: Application, enforce: bool = True, only: Optional[List[str]] = None
    ) -> List[UsageSnapshot]:
        async with self.lock:
            await self._handle_month_change(app, self.billing_now().strftime("%Y-%m"))
            enabled = self.enabled_instances()
            if only is not None:
                wanted = set(only)
                enabled = [x for x in enabled if x["id"] in wanted]
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
                    # A lone blip (or anything under the threshold) stays off
                    # the notification channel entirely — the panel card shows
                    # the streak, and it clears itself on recovery.
                    streak = int(st.get("error_streak", 1) or 1)
                    threshold = int(self.config.monitor["error_notify_after_failures"])
                    # A blip that clears on the next check is not an event —
                    # logging every one of them fills all 50 slots with
                    # failure/recovery pairs and pushes the breaker trips and
                    # config changes (the things worth keeping) off the list.
                    if streak == threshold:
                        self.log_event(
                            f"{inst['name']} 连续 {streak} 次查询失败：{str(snap.error)[:60]}"
                        )
                        st["outage_logged"] = True
                    cooldown = int(self.config.monitor["error_notify_cooldown_seconds"])
                    if (
                        streak >= threshold
                        and time.time() - float(st.get("last_error_notify", 0)) >= cooldown
                    ):
                        since = float(st.get("error_since", 0) or 0)
                        msg = await self.send_notify(
                            app,
                            f"⚠️ <b>{html.escape(inst['name'])} 持续查询失败</b>\n"
                            f"已连续失败 {streak} 次（自 {self.fmt_time(since or time.time())} 起）\n"
                            f"<code>{html.escape(snap.error[:800])}</code>\n\n"
                            f"查询失败期间不会执行自动关机，面板继续显示上次成功数据；"
                            f"恢复后本条通知会自动撤回。",
                        )
                        if msg:
                            # Bots can only delete messages younger than ~48h,
                            # so an unbounded list just accumulates undeletable
                            # references across a long outage.
                            refs = st.setdefault("error_notify_msgs", [])
                            refs.append({"chat_id": msg.chat_id, "message_id": msg.message_id})
                            del refs[:-5]
                        st["last_error_notify"] = time.time()
                        st["error_notified"] = True
                    self.state.save()
                    continue

                recovered = st.pop("recovered", None)
                if recovered is not None:
                    # Recovery is silent: retract the failure notices we sent
                    # (if any) and let the panel going back to normal tell the
                    # rest of the story.
                    since = float(recovered.get("since", 0) or 0)
                    minutes = max(1, int((float(recovered.get("at", time.time())) - since) / 60)) if since else 1
                    if st.pop("outage_logged", False):
                        self.log_event(
                            f"{inst['name']} 查询恢复（失败 {recovered.get('streak', 1)} 次，"
                            f"约 {minutes} 分钟）"
                        )
                    for ref in st.pop("error_notify_msgs", []) or []:
                        try:
                            await app.bot.delete_message(
                                chat_id=ref["chat_id"], message_id=ref["message_id"]
                            )
                        except Exception as exc:
                            self.log.info("撤回失败通知未成功（可能已超时或被删）: %s", exc)

                if st.get("pending_auto_start"):
                    resume_below = float(self.config.monitor["resume_below_percent"])
                    if snap.percent <= resume_below and snap.status == "Stopped":
                        try:
                            await self.api_control(inst, "start")
                            st["pending_auto_start"] = False
                            self.log_event(f"{inst['name']} 新账期自动开机")
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
                        self.log_event(f"{inst['name']} 流量达到 {level}% 提醒线")
                        await self.send_notify(app, self.format_alert(inst, snap, f"达到 {level}% 提醒线"))
                        warned.add(level)
                st["warned_levels"] = sorted(warned)

                # Double-threshold breaker: the soft line needs two
                # consecutive successful readings over it (guards against a
                # single bad API answer or a mis-set quota killing a box),
                # the emergency line fires on one.
                soft = int(inst.get("shutdown_percent", 95))
                hard = self.hard_percent(inst)
                if (
                    enforce
                    and inst.get("auto_shutdown", True)
                    and not st.get("shutdown_triggered", False)
                ):
                    reason = ""
                    if snap.percent >= hard:
                        reason = f"已达紧急熔断线 {hard}%"
                    elif snap.percent >= soft:
                        over = int(st.get("over_threshold_checks", 0) or 0) + 1
                        st["over_threshold_checks"] = over
                        if over >= 2:
                            reason = f"连续 {over} 次确认超过熔断线 {soft}%"
                        else:
                            self.log_event(
                                f"{inst['name']} 达到熔断线 {soft}%，等待下次检查复核"
                            )
                    else:
                        st["over_threshold_checks"] = 0

                    if reason:
                        st["over_threshold_checks"] = 0
                        if snap.status == "Running":
                            try:
                                await self.api_control(inst, "stop")
                                st["shutdown_triggered"] = True
                                self.log_event(f"{inst['name']} 自动关机（{reason}）")
                                await self.send_notify(
                                    app,
                                    self.format_alert(inst, snap, f"已触发自动关机 🛑（{reason}）"),
                                )
                            except Exception as exc:
                                self.log_event(f"{inst['name']} 自动关机失败：{str(exc)[:80]}")
                                await self.send_notify(
                                    app, self.format_alert(inst, snap, f"自动关机失败：{exc}")
                                )
                        elif snap.status == "Stopped":
                            st["shutdown_triggered"] = True
                            self.log_event(f"{inst['name']} 流量超阈值（{reason}），实例已处于关机状态")
                            await self.send_notify(
                                app, self.format_alert(inst, snap, "流量已超阈值，实例当前已关机")
                            )
                        elif snap.status_error:
                            # Traffic is over the line but the power state is
                            # unreadable, so an automatic stop cannot be
                            # confirmed safe. Escalate to a human instead of
                            # silently doing nothing.
                            self.log.warning(
                                "%s 已超阈值但状态读取失败，未发送停机: %s",
                                inst["name"],
                                snap.status_error,
                            )
                            if not st.get("unknown_status_notified"):
                                self.log_event(f"{inst['name']} 超阈值但状态未知，未自动关机")
                                await self.send_notify(
                                    app,
                                    f"⚠️ <b>{html.escape(inst['name'])} 需要人工确认</b>\n"
                                    f"流量已达 {snap.percent:.1f}%（{reason}），"
                                    f"但实例状态读取失败，无法确认是否在运行，"
                                    f"因此<b>没有</b>执行自动关机。\n"
                                    f"<code>{html.escape(snap.status_error[:300])}</code>\n\n"
                                    f"请到阿里云控制台确认，或在面板里手动关机。",
                                )
                                st["unknown_status_notified"] = True
                            st["over_threshold_checks"] = 2  # stay armed
                        else:
                            self.log.warning(
                                "%s 已超阈值但状态为 %s，暂不发送停机", inst["name"], snap.status
                            )

                self.state.save()
            return snapshots

    async def monitor_loop(self, app: Application) -> None:
        await asyncio.sleep(3)
        while True:
            # Cadence is measured from the start of each cycle, so a slow
            # check (a failing instance can spend two minutes in retries)
            # eats into the idle time rather than pushing every later cycle
            # further and further behind.
            started = time.monotonic()
            try:
                self.sync_config()
                snapshots = await self.check_once(app, enforce=True)
                await self.maybe_daily_report(app, snapshots)
                await self.update_panel(app)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("监控循环异常")

            interval = int(self.config.monitor["interval_seconds"])
            retry_delay = int(self.config.monitor["retry_delay_seconds"])
            # Observed failures are almost always a single wobbly minute that
            # has cleared long before the next scheduled check. Re-querying
            # just the failed instances shortly after turns a "5 分钟 outage"
            # into a ~45 秒 one, and usually keeps it below the notification
            # threshold entirely. Healthy instances are not re-queried.
            pending = self.failing_ids()
            if pending and time.monotonic() - started + retry_delay < interval:
                await asyncio.sleep(retry_delay)
                try:
                    self.log.info("对 %d 台失败实例发起快速重试", len(pending))
                    await self.check_once(app, enforce=True, only=pending)
                    await self.update_panel(app)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.log.exception("快速重试异常")
            await asyncio.sleep(max(1.0, interval - (time.monotonic() - started)))

    # -- live panel --------------------------------------------------------

    def register_panel(self, chat_id: int, message_id: int, view: str, page: int = 0) -> None:
        """Remember which message the user is looking at and which screen it
        shows. The monitor loop only redraws it while it shows the home view,
        so a background refresh never yanks the user out of a submenu or an
        input prompt."""
        self.state.data["panel"] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "view": view,
            "page": int(page),
        }
        self.state.save()

    async def update_panel(self, app: Application) -> None:
        panel = self.state.data.get("panel") or {}
        if panel.get("view") != "home" or not panel.get("message_id"):
            return
        page = int(panel.get("page", 0))
        try:
            await app.bot.edit_message_text(
                chat_id=panel["chat_id"],
                message_id=panel["message_id"],
                text=self.home_text(page),
                parse_mode=ParseMode.HTML,
                reply_markup=self.home_keyboard(page),
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            # Message deleted, or older than Telegram's ~48h edit window:
            # unbind and wait for the next /menu to re-register a panel.
            self.state.data.pop("panel", None)
            self.state.save()
            self.log.info("面板自动刷新解绑（%s），等待下次 /menu", exc)
        except Exception as exc:
            self.log.warning("面板自动刷新失败: %s", exc)

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
            + self.card(
                [
                    self.meter(
                        snap.percent,
                        severity(snap.percent, int(inst.get("shutdown_percent", 95))),
                    ),
                    f"{fmt_gb(snap.used_bytes, unit=False)} / {fmt_gb(snap.total_bytes)} · "
                    f"余 {fmt_gb(snap.remaining_bytes)}",
                    f"状态 {html.escape(status_cn(snap.status))}",
                    f"动作 <b>{html.escape(action)}</b>",
                ]
            )
            + f"\n<i>{html.escape(snap.scope_note)}</i>"
        )

    def forecast(self, inst: Dict[str, Any], snap: UsageSnapshot) -> Dict[str, Any]:
        """Burn-rate forecast from rolling 24h/7d rates over the sampled
        history, falling back to the month-to-date average when history is
        thin. rate = max(24h, 7d) so a sudden surge shows up quickly.

        Because of that max(), the headline rate is deliberately the *fastest*
        recent pace, not a month average — a machine that has used little this
        month but sped up yesterday will show a high rate, which is the point.
        `window` says which measurement won so the panel can label it honestly
        instead of calling all of them '日均'."""
        soft = int(inst.get("shutdown_percent", 95))
        # Billing time, not display time: the elapsed-days and days-to-reset
        # maths must line up with when Aliyun actually rolls the quota over.
        now = self.billing_now()
        now_ts = time.time()
        hist = self.state.instance(inst["id"]).get("usage_history") or []
        rate24 = rolling_rate(hist, now_ts, 86400)
        rate7 = rolling_rate(hist, now_ts, 7 * 86400)
        month_rate, _ = burn_forecast(snap.used_bytes, snap.total_bytes, soft, now)
        month_rate = month_rate if month_rate > 0 else None

        window = "month"
        candidates = [(r, w) for r, w in ((rate24, "24h"), (rate7, "7d")) if r is not None]
        if candidates:
            rate, window = max(candidates)
        else:
            rate = month_rate

        reset_at, _ = month_reset_info(now)
        days_to_reset = max(0.0, (reset_at - now).total_seconds() / 86400)
        end_bytes = snap.used_bytes + rate * days_to_reset if rate else None
        threshold_bytes = snap.total_bytes * soft / 100.0

        hit_at: Optional[datetime] = None
        reached = snap.total_bytes > 0 and snap.used_bytes >= threshold_bytes
        if not reached and rate and snap.total_bytes > 0:
            days = (threshold_bytes - snap.used_bytes) / rate
            if days <= days_to_reset:
                hit_at = now + timedelta(days=days)
        return {
            "rate": rate,
            "rate24": rate24,
            "rate7": rate7,
            "month_rate": month_rate,
            "window": window,
            "elapsed_days": (now.day - 1) + now.hour / 24 + now.minute / 1440,
            "end_bytes": end_bytes,
            "hit_at": hit_at,
            "reached": reached,
        }

    RATE_WINDOW_CN = {"24h": "近 24 小时", "7d": "近 7 天", "month": "本月累计"}

    def pace_lines(
        self, inst: Dict[str, Any], snap: UsageSnapshot, risk: bool = True
    ) -> List[str]:
        """Burn rate, plus a risk line only when there is actually a risk.

        Silence is the good news here: dropping '无触线风险' from every healthy
        card keeps the panel short and makes the warning impossible to miss."""
        f = self.forecast(inst, snap)
        if not f["rate"]:
            return []
        # Name the window. "日均 4.8 GB" on a machine that has used 48 GB in 28
        # days reads as a bug; "近 24 小时 4.8 GB/日" reads as what it is.
        pace = f"📈 {self.RATE_WINDOW_CN[f['window']]} {fmt_gb(f['rate'], 1, unit=False)} GB/日"
        if f["end_bytes"] is not None:
            pace += f" · 月底约 {fmt_gb(f['end_bytes'], 0, unit=False)} GB"
        lines = [pace]
        if not risk:
            return lines
        if f["reached"]:
            lines.append("🛑 已达熔断线")
        elif f["hit_at"] is not None:
            lines.append(f"⚠️ 预计 {f['hit_at']:%m-%d} 触及熔断线")
        return lines

    def pct_icon(self, percent: float, shutdown_percent: int) -> str:
        return SEV_ICON[severity(percent, shutdown_percent)]

    def meter(self, percent: float, level: str = "ok", width: int = BAR_WIDTH) -> str:
        """Bar plus percentage, as an HTML fragment.

        The bar is a fixed number of cells, so the percentage starts at the
        same column on every card without padding the number itself — padding
        it looked like a stray gap whenever nothing was over 100%."""
        style = str(self.config.monitor.get("bar_style", "bars"))
        bar = progress_bar(percent, width, style, level)
        if style == "squares":
            # Emoji in a monospace run get letter-spaced by some clients.
            return f"{bar} <b>{percent:.1f}%</b>"
        return f"<code>{bar}</code> <b>{percent:.1f}%</b>"

    def card(self, lines: List[str]) -> str:
        """Wrap one machine card according to the chosen layout. Telegram's
        blockquote gives a tinted panel with a quote glyph on some clients,
        which reads as 'someone said this' rather than 'this is a card' — the
        flat layout drops it for anyone who prefers plainer output."""
        body = "\n".join(lines)
        if str(self.config.monitor.get("panel_style", "card")) == "flat":
            return body
        return f"<blockquote>{body}</blockquote>"

    @staticmethod
    def hard_percent(inst: Dict[str, Any]) -> int:
        """Emergency breaker: one confirmed reading at/above this fires
        immediately, no second-cycle confirmation. Never below the soft line."""
        soft = int(inst.get("shutdown_percent", 95))
        return max(soft, min(99, int(inst.get("emergency_shutdown_percent", 98))))

    def instance_block(self, inst: Dict[str, Any]) -> str:
        """One machine card for the home panel, rendered as a Telegram
        blockquote so each machine reads as a visually separated card instead
        of one undifferentiated wall of lines."""
        name = html.escape(inst["name"])
        snap = self.last_snapshots.get(inst["id"])
        st = self.state.instance(inst["id"])
        streak = int(st.get("error_streak", 0) or 0)

        if snap is None or snap.error:
            # No good reading to fall back on (legacy state files may still
            # carry an error snapshot here).
            err = str(st.get("last_error") or (snap.error if snap else "") or "")
            if streak or err:
                head = f"🔴 <b>{name}</b> · 查询失败"
                if streak:
                    head += f"（{streak} 次 · {self.fmt_clock(float(st.get('last_error_at', 0)))}）"
                return self.card([head, f"<code>{html.escape(err[:140])}</code>"])
            return self.card([f"⚪ <b>{name}</b> · 尚未查询", "<i>点下方「刷新全部」开始</i>"])

        threshold = int(inst.get("shutdown_percent", 95))
        level = severity(snap.percent, threshold)
        tripped = bool(st.get("shutdown_triggered"))
        state_cn = "状态未知" if snap.status_error else status_cn(snap.status)
        lines = [f"{SEV_ICON[level]} <b>{name}</b> · {html.escape(state_cn)}"]
        if snap.total_bytes <= 0:
            # A zero quota (mis-set quota_gb, or a SWAS instance with no
            # traffic package) makes every percentage meaningless — say so
            # instead of rendering a confident 0.0%.
            lines += [
                f"已用 {fmt_gb(snap.used_bytes)}",
                "⚠️ 额度读数为 0，无法计算百分比",
            ]
        else:
            lines += [
                self.meter(snap.percent, level),
                f"{fmt_gb(snap.used_bytes, unit=False)} / {fmt_gb(snap.total_bytes)} · "
                f"余 {fmt_gb(snap.remaining_bytes)}",
            ]
            # Once the breaker has latched, the '已达熔断线' risk line just
            # repeats what the 已触发 line below says.
            lines += self.pace_lines(inst, snap, risk=not tripped)

        shield = f"🛡 熔断 {threshold}% {'开' if inst.get('auto_shutdown', True) else '关'}"
        resume = f"🗓 次月开机 {'开' if inst.get('auto_start_next_month') else '关'}"
        if tripped:
            lines += [shield, f"🛑 本账期已触发熔断 · {resume}"]
        else:
            lines.append(f"{shield} · {resume}")

        if snap.overflow_bytes > 0:
            lines.append(f"❗ 已超额 {fmt_gb(snap.overflow_bytes)}")
        if snap.status_error:
            lines.append("⚠️ 开关机状态读取失败，流量数据正常")
        if streak:
            lines.append(
                f"⚠️ 查询异常 {streak} 次 · 以上为 {self.fmt_clock(snap.checked_at)} 数据"
            )
        return self.card(lines)

    def page_instances(self, page: int) -> Tuple[int, int, List[Dict[str, Any]]]:
        enabled = self.enabled_instances()
        page_size = int(self.config.monitor["telegram_page_size"])
        total_pages = max(1, (len(enabled) + page_size - 1) // page_size)
        page = min(max(0, page), total_pages - 1)
        start = page * page_size
        return page, total_pages, enabled[start : start + page_size]

    def pool_total_line(self) -> str:
        """Account-level roll-up, shown only when it means something.

        CDT quotas are per account and per scope, so two machines sharing one
        AccessKey report the *same* pool — summing them would double-count.
        Those are collapsed to a single contribution here."""
        seen: set = set()
        used = total = 0
        pools = 0
        for inst in self.enabled_instances():
            snap = self.last_snapshots.get(inst["id"])
            if snap is None or snap.error or snap.total_bytes <= 0:
                return ""  # an incomplete total is worse than none
            if inst.get("provider") == "ecs_cdt":
                key = ("cdt", inst.get("access_key_id"), inst.get("traffic_scope", "overseas"))
            else:
                key = ("swas", inst["id"])
            if key in seen:
                continue
            seen.add(key)
            used += snap.used_bytes
            total += snap.total_bytes
            pools += 1
        if pools < 2 or total <= 0:
            return ""
        percent = used / total * 100
        # Reuse the worst per-machine severity so the roll-up meter cannot look
        # calm while one machine is already at its breaker.
        worst = "ok"
        for inst in self.enabled_instances():
            snap = self.last_snapshots.get(inst["id"])
            if snap is None or snap.total_bytes <= 0:
                continue
            level = severity(snap.percent, int(inst.get("shutdown_percent", 95)))
            if level == "crit" or (level == "warn" and worst == "ok"):
                worst = level
        return (
            f"{self.meter(percent, worst)} · "
            f"{fmt_gb(used, 1, unit=False)} / {fmt_gb(total, 0)}"
        )

    def freshness(self) -> str:
        interval = int(self.config.monitor["interval_seconds"])
        checked = [s.checked_at for s in self.last_snapshots.values() if s.checked_at]
        if not checked:
            return "⚪ 尚未查询"
        latest = max(checked)
        age = time.time() - latest
        # ≤2 cycles: live; ≤6: delayed; beyond: stale — tells "traffic is
        # safe" apart from "the monitor itself is unhealthy" at a glance.
        dot = "🟢" if age <= 2 * interval else ("🟡" if age <= 6 * interval else "🔴")
        stamp = datetime.fromtimestamp(latest, self.tz)
        same_day = stamp.date() == self.now().date()
        return f"{dot} {stamp:%H:%M}" if same_day else f"{dot} {stamp:%m-%d %H:%M}"

    def home_text(self, page: int = 0, title: str = "📊 阿里云流量监控", notice: str = "") -> str:
        page, total_pages, items = self.page_instances(page)
        billing = self.billing_now()
        _, days_left = month_reset_info(billing)
        interval = int(self.config.monitor["interval_seconds"])
        failing = sum(
            1
            for inst in self.enabled_instances()
            if int(self.state.instance(inst["id"]).get("error_streak", 0) or 0) > 0
        )

        cycle = f"账期 {billing:%Y-%m} · {days_left} 天后重置"
        if total_pages > 1:
            cycle += f" · {page + 1}/{total_pages} 页"
        watch = f"🛰 每 {self.fmt_interval(interval)}检查 · {self.freshness()}"
        if failing:
            watch += f" · ⚠️ {failing} 台异常"

        header = [f"<b>{title}</b>", cycle, watch]
        rollup = self.pool_total_line()
        if rollup:
            header.append(rollup)
        if notice:
            header.append(notice)

        if not items:
            header.append("\n<i>暂无已启用机器。在管理服务器运行 aliyun-monitor 添加。</i>")
            return "\n".join(header)

        head = "\n".join(header)
        disabled = [x for x in self.config.instances if not x.get("enabled", True)]
        footer = ""
        if disabled:
            names = "、".join(html.escape(x["name"]) for x in disabled)
            footer = f"\n\n<i>⏸ 已停用：{names}</i>"

        # Telegram rejects an over-long edit outright, which would take the
        # whole panel down. Drop trailing cards instead of the whole message —
        # truncating the string itself would slice a blockquote open and break
        # HTML parsing too.
        budget = HOME_TEXT_BUDGET - len(head) - len(footer)
        blocks: List[str] = []
        for inst in items:
            block = self.instance_block(inst)
            if blocks and len(block) + 2 > budget:
                break
            blocks.append(block)
            budget -= len(block) + 2
        if len(blocks) < len(items):
            footer = (
                f"\n\n<i>⚠️ 还有 {len(items) - len(blocks)} 台未显示，"
                f"可调小「每页机器数」。</i>" + footer
            )
        return head + "\n\n" + "\n\n".join(blocks) + footer

    def instance_text(self, inst: Dict[str, Any], notice: str = "") -> str:
        name = html.escape(inst["name"])
        snap = self.last_snapshots.get(inst["id"])
        st = self.state.instance(inst["id"])
        streak = int(st.get("error_streak", 0) or 0)
        good = snap is not None and not snap.error
        soft = int(inst.get("shutdown_percent", 95))

        head = f"🖥 <b>{name}</b>"
        if good:
            state_cn = "状态未知" if snap.status_error else status_cn(snap.status)
            head += f" · {self.pct_icon(snap.percent, soft)} {html.escape(state_cn)}"
        if not inst.get("enabled", True):
            head += " · ⏸ 已停用监控"
        lines = [head]
        if notice:
            lines.append(notice)

        if good and snap.total_bytes > 0:
            usage = [
                self.meter(snap.percent, severity(snap.percent, soft), DETAIL_BAR_WIDTH),
                f"已用 <b>{fmt_gb(snap.used_bytes)}</b> / {fmt_gb(snap.total_bytes)}",
                f"剩余 <b>{fmt_gb(snap.remaining_bytes)}</b>",
            ]
            if snap.overflow_bytes > 0:
                usage.append(f"❗ 已超额 <b>{fmt_gb(snap.overflow_bytes)}</b>")
            lines += ["", self.card(usage)]
        elif good:
            lines += ["", f"已用 <b>{fmt_gb(snap.used_bytes)}</b>", "⚠️ 额度读数为 0，无法计算百分比"]

        if good:
            f = self.forecast(inst, snap)
            # All three rates, always, with the one driving the forecast
            # marked. Showing a single number invites "why is the machine that
            # used less showing a bigger average?" — because it is not an
            # average of the month, it is how fast it is going right now.
            forecast_lines: List[str] = []
            for key, window in (("rate24", "24h"), ("rate7", "7d"), ("month_rate", "month")):
                value = f[key]
                if value is None:
                    continue
                row = f"{self.RATE_WINDOW_CN[window]} <b>{fmt_gb(value, 1, unit=False)}</b> GB/日"
                if window == "month":
                    row += f"（{fmt_gb(snap.used_bytes, 1)} ÷ {f['elapsed_days']:.1f} 天）"
                if window == f["window"]:
                    row += " ← 用于预测"
                forecast_lines.append("📈 " + row)
            if f["end_bytes"] is not None:
                if f["reached"]:
                    risk = "🛑 已达熔断线"
                elif f["hit_at"] is not None:
                    risk = f"⚠️ 预计 {f['hit_at']:%m-%d} 触线"
                else:
                    risk = "✅ 无触线风险"
                forecast_lines.append(f"🔮 月底约 {fmt_gb(f['end_bytes'], 0)} · {risk}")
            if forecast_lines:
                lines.append(self.card(forecast_lines))

        guard = [
            f"🛡 熔断 {soft}% {'开' if inst.get('auto_shutdown', True) else '关'}"
            + ("（连续 2 次确认）" if inst.get("auto_shutdown", True) else ""),
        ]
        if inst.get("auto_shutdown", True):
            guard.append(f"🚨 紧急 {self.hard_percent(inst)}%（单次即关机）")
            if good and snap.total_bytes > 0:
                guard.append(f"⛔ 关机线约 {fmt_gb(snap.total_bytes * soft / 100.0)}")
        if st.get("shutdown_triggered"):
            guard.append("🛑 本账期已触发（新账期自动解除）")
        guard.append(
            f"🗓 次月开机 {'开' if inst.get('auto_start_next_month') else '关'} · "
            f"🎛 手动控制 {'开' if inst.get('allow_manual_control', True) else '关'}"
        )
        lines.append(self.card(guard))

        if good and snap.status_error:
            lines.append(
                self.card(
                    [
                        "⚠️ <b>开关机状态读取失败</b>",
                        f"<code>{html.escape(snap.status_error[:300])}</code>",
                        "<i>流量数据正常；状态未知期间不会自动关机，"
                        "超过熔断线会改为发通知提醒人工处理。</i>",
                    ]
                )
            )
        if streak or (snap is not None and snap.error):
            err = str(st.get("last_error") or (snap.error if snap else "") or "")
            when = (
                f"（连续 {streak} 次 · 最近 {self.fmt_clock(float(st.get('last_error_at', 0)))}）"
                if streak
                else ""
            )
            fail = [f"⚠️ <b>查询失败中</b>{when}", f"<code>{html.escape(err[:400])}</code>"]
            if good:
                fail.append("<i>上方用量为最后一次成功查询的数据。</i>")
            lines.append(self.card(fail))
        elif snap is None:
            lines.append("<i>尚未查询，点击「🔄 刷新」</i>")

        source = (
            "ListCdtInternetTraffic"
            if inst.get("provider") == "ecs_cdt"
            else "ListInstancesTrafficPackages"
        )
        meta = [
            "",
            f"<i>{PROVIDERS[inst['provider']]} · {html.escape(inst['region'])} · "
            f"<code>{html.escape(inst['instance_id'])}</code></i>",
            "<i>🔒 停机保留公网 IP（KeepCharging，不可更改）</i>",
        ]
        if inst.get("provider") == "ecs_cdt":
            meta.append(
                f"<i>额度 {float(inst.get('quota_gb', 0)):g} GB（GiB 口径）· 数据源 {source}</i>"
            )
        else:
            meta.append(f"<i>数据源 {source}</i>")
        if good:
            meta.append(f"<i>{html.escape(snap.scope_note)}</i>")
            meta.append(f"<i>更新于 {self.fmt_time(snap.checked_at)}</i>")
        twins = [
            html.escape(x["name"])
            for x in self.enabled_instances()
            if x["id"] != inst["id"]
            and x.get("provider") == "ecs_cdt"
            and inst.get("provider") == "ecs_cdt"
            and x.get("access_key_id") == inst.get("access_key_id")
            and x.get("traffic_scope", "overseas") == inst.get("traffic_scope", "overseas")
        ]
        if twins:
            meta.append(f"<i>⚠️ 与 {'、'.join(twins)} 共用账号与口径，流量为账号级共享值</i>")
        return "\n".join(lines + meta)

    def global_text(self) -> str:
        m = self.config.monitor
        daily = m.get("daily_report_time") or "已关闭"
        return "\n".join(
            [
                "🛠 <b>全局设置</b>",
                "",
                "<blockquote>"
                f"⏱ 检查间隔 <b>{self.fmt_interval(int(m['interval_seconds']))}</b>\n"
                f"🔔 分级提醒线 <b>{'/'.join(str(x) for x in m['warning_percentages'])}%</b>\n"
                f"📅 每日汇总 <b>{html.escape(str(daily))}</b>\n"
                f"▶️ 新账期开机确认线 <b>≤ {m['resume_below_percent']}%</b>\n"
                f"♻️ 查询失败后 <b>{m['retry_delay_seconds']} 秒</b>快速重试\n"
                f"🔕 连续 <b>{m['error_notify_after_failures']}</b> 次失败才提醒，恢复自动撤回"
                "</blockquote>",
                "<blockquote>"
                f"🎨 面板布局 <b>{PANEL_STYLE_CN[str(m['panel_style'])]}</b> · "
                f"进度条 <b>{BAR_STYLE_CN[str(m['bar_style'])]}</b>\n"
                f"🌏 显示时区 <code>{html.escape(str(m['timezone']))}</code>\n"
                f"🧾 账期时区 <code>Asia/Shanghai</code>（阿里云计费）\n"
                f"⚡ 并发查询 <b>{m['max_concurrency']}</b> · 每页 <b>{m['telegram_page_size']}</b> 台"
                "</blockquote>",
                "<blockquote>"
                f"🤖 运行时长 {self.fmt_uptime()}\n"
                f"📡 24h API 成功率 {self.fmt_api_rate()}\n"
                f"🖥 共 {len(self.config.instances)} 台 · 启用 {len(self.enabled_instances())} 台"
                "</blockquote>",
                f"<i>版本 {VERSION}</i>",
            ]
        )

    def fmt_uptime(self) -> str:
        seconds = int(time.time() - self.started_at)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分钟"
        return f"{max(1, minutes)} 分钟"

    def fmt_api_rate(self) -> str:
        ok, fail = self.api_stats_24h()
        total = ok + fail
        if not total:
            return "暂无数据"
        return f"{ok / total * 100:.1f}%（{ok}/{total} 次，失败 {fail} 次）"

    def events_text(self) -> str:
        events = list(reversed(self.state.data.get("events") or []))[:15]
        lines = ["📋 <b>事件记录</b>", ""]
        if not events:
            lines.append("<i>暂无事件。熔断、开关机、阈值修改、查询异常等关键动作会记录在这里。</i>")
            return "\n".join(lines)
        rows = []
        for e in events:
            stamp = self.fmt_time(float(e.get("t", 0)))[5:]  # MM-DD HH:MM
            rows.append(f"<code>{stamp}</code> {html.escape(str(e.get('text', '')))}")
        lines.append("<blockquote>" + "\n".join(rows) + "</blockquote>")
        lines += ["", "<i>保留最近 50 条，此处显示 15 条；例行的成功查询不记录。</i>"]
        return "\n".join(lines)

    @staticmethod
    def events_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 刷新记录", callback_data="n:e")],
                [InlineKeyboardButton(HOME, callback_data="n:h:0")],
            ]
        )

    def admins_text(self, current_user: int) -> str:
        admins = self.config.telegram["admin_user_ids"]
        rows = []
        for uid in admins:
            mark = " ← 你" if uid == current_user else ""
            notify = " · 🔔 接收通知" if uid == self.config.telegram["notify_chat_id"] else ""
            rows.append(f"<code>{uid}</code>{mark}{notify}")
        return "\n".join(
            [
                "👥 <b>管理员</b>",
                "",
                "<blockquote>" + "\n".join(rows) + "</blockquote>",
                "<i>管理员可以查看面板并控制实例。</i>",
            ]
        )

    # -- keyboards ---------------------------------------------------------

    def home_keyboard(self, page: int = 0) -> InlineKeyboardMarkup:
        page, total_pages, items = self.page_instances(page)
        rows: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton("🔄 刷新全部", callback_data=f"r:a:{page}"),
                InlineKeyboardButton("📋 事件记录", callback_data="n:e"),
            ]
        ]
        # Power buttons live only inside the per-machine page (with its own
        # confirmation step) — the home view is a read-only overview.
        row: List[InlineKeyboardButton] = []
        for inst in items:
            key = inst["id"]
            snap = self.last_snapshots.get(key)
            if snap and not snap.error and snap.total_bytes > 0:
                icon = self.pct_icon(snap.percent, int(inst.get("shutdown_percent", 95)))
                label = f"{icon} {short_name(inst['name'])} {snap.percent:.0f}%"
            elif snap and not snap.error:
                label = f"⚙️ {short_name(inst['name'])}"
            else:
                label = f"🔴 {short_name(inst['name'])}"
            row.append(InlineKeyboardButton(label, callback_data=f"n:i:{key}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        # Disabled machines used to disappear from the panel entirely, which
        # meant re-enabling one required SSH into the management box.
        row = []
        for inst in [x for x in self.config.instances if not x.get("enabled", True)]:
            row.append(
                InlineKeyboardButton(
                    f"⏸ {short_name(inst['name'])}", callback_data=f"n:i:{inst['id']}"
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
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
                InlineKeyboardButton("🛠 全局设置", callback_data="n:g"),
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
                    f"🛡 自动熔断：{'开' if inst.get('auto_shutdown', True) else '关'}",
                    callback_data=f"t:{key}:auto_shutdown",
                )
            ],
            [
                InlineKeyboardButton("－5", callback_data=f"p:{key}:-5"),
                InlineKeyboardButton("－1", callback_data=f"p:{key}:-1"),
                InlineKeyboardButton(f"🛡 {threshold}%", callback_data="nop"),
                InlineKeyboardButton("＋1", callback_data=f"p:{key}:1"),
                InlineKeyboardButton("＋5", callback_data=f"p:{key}:5"),
            ],
            [
                InlineKeyboardButton("－1", callback_data=f"P:{key}:-1"),
                InlineKeyboardButton(f"🚨 紧急 {self.hard_percent(inst)}%", callback_data="nop"),
                InlineKeyboardButton("＋1", callback_data=f"P:{key}:1"),
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
                    f"🗓 次月自动开机：{'开' if inst.get('auto_start_next_month') else '关'}",
                    callback_data=f"t:{key}:auto_start_next_month",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🎛 允许手动控制：{'开' if inst.get('allow_manual_control', True) else '关'}",
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
                    InlineKeyboardButton(
                        f"⏱ {self.fmt_interval(int(m['interval_seconds']))}", callback_data="nop"
                    ),
                    InlineKeyboardButton("＋60s", callback_data="g:interval_seconds:60"),
                ],
                [
                    InlineKeyboardButton("－1", callback_data="g:resume_below_percent:-1"),
                    InlineKeyboardButton(
                        f"▶️ 开机确认线 {m['resume_below_percent']}%", callback_data="nop"
                    ),
                    InlineKeyboardButton("＋1", callback_data="g:resume_below_percent:1"),
                ],
                [
                    InlineKeyboardButton("－1", callback_data="g:error_notify_after_failures:-1"),
                    InlineKeyboardButton(
                        f"🔕 失败 {m['error_notify_after_failures']} 次提醒", callback_data="nop"
                    ),
                    InlineKeyboardButton("＋1", callback_data="g:error_notify_after_failures:1"),
                ],
                [
                    InlineKeyboardButton("✏️ 提醒线", callback_data="G:warning_percentages"),
                    InlineKeyboardButton("✏️ 汇总时间", callback_data="G:daily_report_time"),
                ],
                # Cycling in place beats a submenu here: the effect is only
                # visible on the home panel, so you flip and go look.
                [
                    InlineKeyboardButton(
                        f"🎨 {PANEL_STYLE_CN[str(m['panel_style'])]}", callback_data="s:panel_style"
                    ),
                    InlineKeyboardButton(
                        f"{BAR_STYLE_CN[str(m['bar_style'])]}", callback_data="s:bar_style"
                    ),
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
                "<b>面板符号</b>",
                "<blockquote>🟢 / 🟡 / 🔴 是<b>流量风险</b>，不是开关机状态\n"
                "开关机以「运行中 / 已关机」文字为准\n"
                "标题行的 🟢 是<b>数据新鲜度</b>：绿=刚更新，红=监控可能异常</blockquote>",
                "<b>命令</b>",
                "<blockquote>/menu 打开面板\n/status 刷新并打开面板\n"
                "/id 查看自己的 Telegram ID</blockquote>",
                "<b>计量口径</b>",
                "<blockquote>ECS/CDT 读取的是<b>整个阿里云账号</b>的 CDT 流量池，"
                "不是单块网卡。一个账号只跑一台主力机时最准确。\n"
                "账期按阿里云计费时区（Asia/Shanghai）翻月。</blockquote>",
                "<b>速度与预测</b>",
                "<blockquote>卡片上的速度是<b>最近的跑量速度</b>，取「近 24 小时」与"
                "「近 7 天」中<b>较大</b>的一个——突然开始跑量要立刻反映出来，"
                "所以宁可偏高。\n"
                "它<b>不是</b>本月累计平均值。本月用得少但昨天开始猛跑的机器，"
                "这个数就会明显高于「累计 ÷ 天数」，属于正常。\n"
                "三个口径都在机器详情页里，可以直接对比。</blockquote>",
                "<b>熔断规则</b>",
                "<blockquote>• 流量查询失败时<b>绝不</b>自动关机\n"
                "• 普通熔断线需连续 2 次确认，紧急线单次即关\n"
                "• 实例状态不是运行中时不会重复发关机指令\n"
                "• 同账期熔断一次后不再重复触发\n"
                "• 手动开机时若流量仍超阈值且熔断开启，会被拦截</blockquote>",
                "<b>安全边界</b>",
                "<blockquote>• 本工具<b>没有</b>删除实例、释放或解绑 EIP 的能力：代码只调用"
                "读流量、读状态和开关机 API，RAM 策略同时显式 Deny 删除实例与释放 EIP\n"
                "• 关机固定 KeepCharging，公网 IP 与资源全部保留\n"
                "• 新增/删除机器、修改 AccessKey 只能在管理服务器上运行 "
                "<code>aliyun-monitor</code>，密钥不经过 Telegram</blockquote>",
            ]
        )


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


async def safe_edit(query, text: str, markup: Optional[InlineKeyboardMarkup]) -> None:
    """Edit in place, tolerating Telegram's 'message is not modified' error
    which fires whenever a refresh produces byte-identical output.

    A rejected edit used to leave the panel frozen on whatever it showed
    before, so formatting problems fall back to plain text rather than
    stranding the user."""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
        return
    except BadRequest as exc:
        reason = str(exc).lower()
        if "not modified" in reason:
            return
        if "message to edit not found" in reason or "message can't be edited" in reason:
            logging.getLogger("monitor").info("面板消息已不可编辑: %s", exc)
            return
        logging.getLogger("monitor").warning("面板渲染被拒绝，降级为纯文本: %s", exc)
    plain = strip_tags(text)[: TG_TEXT_LIMIT - 1]
    try:
        await query.edit_message_text(plain, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logging.getLogger("monitor").error("面板纯文本降级仍失败: %s", exc)


async def render_home(service: MonitorService, query, page: int = 0, notice: str = "") -> None:
    service.register_panel(query.message.chat_id, query.message.message_id, "home", page)
    await safe_edit(query, service.home_text(page, notice=notice), service.home_keyboard(page))


async def render_instance(
    service: MonitorService, query, inst: Dict[str, Any], notice: str = ""
) -> None:
    service.register_panel(query.message.chat_id, query.message.message_id, "instance")
    await safe_edit(query, service.instance_text(inst, notice=notice), service.instance_keyboard(inst))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def command_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    if not service.is_authorized(update):
        await service.reject(update)
        return
    service.sync_config()
    context.user_data.pop("await", None)
    msg = await update.effective_message.reply_text(
        service.home_text(0),
        parse_mode=ParseMode.HTML,
        reply_markup=service.home_keyboard(0),
        disable_web_page_preview=True,
    )
    service.register_panel(msg.chat_id, msg.message_id, "home", 0)


async def command_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    if not service.is_authorized(update):
        await service.reject(update)
        return
    service.sync_config()
    context.user_data.pop("await", None)
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
    service.register_panel(msg.chat_id, msg.message_id, "home", 0)


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
    # About to mutate and rewrite config.json — adopt any outside edit first
    # rather than overwriting it.
    service.sync_config()
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
                old_quota = float(inst.get("quota_gb", 0))
                inst["quota_gb"] = value
                if value != old_quota:
                    service.log_event(
                        f"管理员将 {inst['name']} 月度额度 {old_quota:g} GB → {value:g} GB"
                    )
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
    """Outer shell: a handler that raises leaves Telegram's button spinner
    running forever with no explanation, so every failure gets answered."""
    query = update.callback_query
    try:
        await dispatch_callback(update, context)
    except Exception:
        logging.getLogger("monitor").exception("回调处理失败: %s", getattr(query, "data", ""))
        if query:
            try:
                await query.answer("⚠️ 操作失败，请重试或发送 /menu 重开面板。", show_alert=True)
            except Exception:
                pass


async def dispatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MonitorService = context.application.bot_data["service"]
    query = update.callback_query
    if not query:
        return
    if not service.is_authorized(update):
        await service.reject(update)
        return
    service.sync_config()

    data = query.data or ""
    parts = data.split(":")
    head = parts[0]

    if data == "nop":
        await query.answer()
        return

    # Any interaction moves the live panel to this message. Default to a
    # non-home view so the background loop keeps its hands off submenus and
    # input prompts; render_home/render_instance immediately re-register the
    # real view for the branches that end on one.
    if query.message:
        service.register_panel(query.message.chat_id, query.message.message_id, "other")

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
        elif target == "e":
            await safe_edit(query, service.events_text(), service.events_keyboard())
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
            # Flip the message into a working state immediately so the press
            # is visibly acknowledged before the (slow) API round-trips.
            await safe_edit(
                query,
                f"⏳ <b>正在查询 {len(service.enabled_instances())} 台机器……</b>\n"
                f"<i>完成后本条消息会自动刷新，无需重复点击。</i>",
                None,
            )
            await service.check_once(context.application, enforce=True)
            await render_home(
                service, query, page, notice=f"✅ 已刷新 · {service.now():%H:%M:%S}"
            )
        else:
            inst = service.config.get_instance(parts[2])
            if not inst:
                await render_home(service, query, 0)
                return
            await safe_edit(
                query, f"⏳ <b>正在查询 {html.escape(inst['name'])}……</b>", None
            )
            await service.api_snapshot(inst)
            await render_instance(
                service, query, inst, notice=f"✅ 已刷新 · {service.now():%H:%M:%S}"
            )
        return

    # -- global settings ---------------------------------------------------
    if head == "g":
        field, delta = parts[1], int(parts[2])
        bounds = {
            "interval_seconds": (60, 86400),
            "resume_below_percent": (0, 50),
            "error_notify_after_failures": (1, 50),
        }
        if field not in bounds:
            await query.answer()
            return
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

    # -- appearance --------------------------------------------------------
    if head == "s":
        field = parts[1]
        options = {"panel_style": list(PANEL_STYLE_CN), "bar_style": list(BAR_STYLES)}
        if field not in options:
            await query.answer()
            return
        choices = options[field]
        current = str(service.config.monitor.get(field, choices[0]))
        index = choices.index(current) if current in choices else 0
        service.config.monitor[field] = choices[(index + 1) % len(choices)]
        service.config.save()
        labels = PANEL_STYLE_CN if field == "panel_style" else BAR_STYLE_CN
        await query.answer(f"已切换为 {labels[service.config.monitor[field]]}")
        await safe_edit(query, service.global_text(), service.global_keyboard())
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
        field_cn = {
            "auto_shutdown": "自动熔断",
            "auto_start_next_month": "下月自动开机",
            "allow_manual_control": "手动控制",
            "enabled": "监控",
        }[field]
        service.log_event(
            f"管理员将 {inst['name']} 的{field_cn}设为{'开' if inst[field] else '关'}"
        )
        await query.answer(f"已{'开启' if inst[field] else '关闭'}")
        await render_instance(service, query, inst)
        return

    if head == "p":
        delta = int(parts[2])
        old = int(inst.get("shutdown_percent", 95))
        inst["shutdown_percent"] = min(100, max(1, old + delta))
        service.config.save()
        if inst["shutdown_percent"] != old:
            service.log_event(
                f"管理员将 {inst['name']} 熔断阈值 {old}% → {inst['shutdown_percent']}%"
            )
        await query.answer(f"熔断线 {inst['shutdown_percent']}%")
        await render_instance(service, query, inst)
        return

    if head == "P":
        delta = int(parts[2])
        old = MonitorService.hard_percent(inst)
        # Clamped to [soft, 99] so the emergency line can never sit below the
        # ordinary one, which would make it fire first and skip confirmation.
        soft = int(inst.get("shutdown_percent", 95))
        inst["emergency_shutdown_percent"] = min(99, max(soft, old + delta))
        service.config.save()
        new = MonitorService.hard_percent(inst)
        if new != old:
            service.log_event(f"管理员将 {inst['name']} 紧急熔断线 {old}% → {new}%")
        await query.answer(f"紧急线 {new}%")
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
        await query.answer(f"正在{ACTION_CN[action]}……")
        if action == "start" and inst.get("auto_shutdown", True):
            await safe_edit(query, "⏳ <b>正在核对当前流量……</b>", None)
            snap = await service.api_snapshot(inst)
            if not snap.error and snap.percent >= int(inst.get("shutdown_percent", 95)):
                await safe_edit(
                    query,
                    f"🚫 <b>{html.escape(inst['name'])}</b> 当前流量 {snap.percent:.1f}%，"
                    f"仍高于熔断阈值 {inst.get('shutdown_percent', 95)}%。\n\n"
                    f"开机后会立刻被再次自动关机。请先关闭自动熔断、调高阈值，或等待新账期。",
                    service.instance_keyboard(inst),
                )
                return
        await safe_edit(
            query,
            f"⏳ <b>正在向阿里云发送{ACTION_CN[action]}指令……</b>",
            None,
        )
        try:
            await service.api_control(inst, action)
            service.log_event(f"管理员手动{ACTION_CN[action]} {inst['name']}")
            note = f"✅ 已发送 <b>{ACTION_CN[action]}</b> 指令 · {service.now():%H:%M:%S}\n<i>实例状态需要几十秒才会变化，稍后点刷新确认。</i>"
        except Exception as exc:
            service.log_event(f"管理员手动{ACTION_CN[action]} {inst['name']} 失败")
            note = f"❌ 操作失败 · {service.now():%H:%M:%S}\n<code>{html.escape(str(exc)[:500])}</code>"
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
    # concurrent_updates: one slow handler (e.g. a refresh waiting on the
    # Aliyun API) must never freeze every other button press in the queue.
    app = (
        ApplicationBuilder()
        .token(config.telegram["bot_token"])
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
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
