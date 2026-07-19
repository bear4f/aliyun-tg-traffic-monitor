#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared model, storage and Aliyun API layer for the traffic monitor.

Both the Telegram bot (app.py) and the terminal panel (setup.py) import this
module so that credential validation, quota maths and formatting behave
identically in both places.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

APP_DIR = Path(os.environ.get("ALIYUN_MONITOR_DIR", "/opt/aliyun-traffic-bot"))
CONFIG_PATH = Path(os.environ.get("ALIYUN_MONITOR_CONFIG", APP_DIR / "config.json"))
STATE_PATH = Path(os.environ.get("ALIYUN_MONITOR_STATE", APP_DIR / "state.json"))
LOG_PATH = Path(os.environ.get("ALIYUN_MONITOR_LOG", APP_DIR / "monitor.log"))

SERVICE_NAME = "aliyun-traffic-bot"
GIB = 1024 ** 3
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
VERSION = "3.0.2"

PROVIDERS = {
    "ecs_cdt": "ECS / CDT",
    "swas": "轻量应用服务器",
}

SCOPES = {
    "overseas": "非中国内地累计流量（香港、新加坡、日本、美国等）",
    "mainland": "中国内地累计流量",
    "all": "账号全部公网累计流量",
    "exact_region": "仅实例所在地域累计流量",
}

# The complete list of Aliyun API actions this tool is able to invoke.
# Anything outside this list (delete instance, release/unbind EIP, resize,
# change billing…) is neither present in code nor granted by the RAM policies.
SAFE_ACTIONS = [
    "cdt:ListCdtInternetTraffic（读流量）",
    "ecs:DescribeInstances（读状态）",
    "ecs:StartInstance / StopInstance / RebootInstance（电源控制）",
    "swas:ListInstancesTrafficPackages / ListInstanceStatus（轻量读取）",
    "swas:StartInstance / StopInstance / RebootInstance（轻量电源控制）",
]


class ConfigError(RuntimeError):
    pass


class AliyunAPIError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Terminal colour helpers
# --------------------------------------------------------------------------


class C:
    """ANSI colours, automatically disabled when stdout is not a terminal."""

    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"

    @classmethod
    def paint(cls, text: str, *codes: str) -> str:
        if not cls.enabled or not codes:
            return text
        return "".join(codes) + text + cls.RESET


def bold(text: str) -> str:
    return C.paint(text, C.BOLD)


def dim(text: str) -> str:
    return C.paint(text, C.GREY)


def ok(text: str) -> str:
    return C.paint(text, C.GREEN)


def warn(text: str) -> str:
    return C.paint(text, C.YELLOW)


def bad(text: str) -> str:
    return C.paint(text, C.RED)


# --------------------------------------------------------------------------
# Formatting shared by the terminal panel and the Telegram panel
# --------------------------------------------------------------------------


def gib(value: int | float) -> float:
    return float(value) / GIB


def fmt_gb(value: int | float, digits: int = 2) -> str:
    return f"{gib(value):.{digits}f} GB"


def progress_bar(percent: float, width: int = 12) -> str:
    """Unicode meter that stays readable in both Telegram and a terminal."""
    ratio = max(0.0, min(1.0, percent / 100.0))
    filled = int(round(ratio * width))
    # Never show a completely empty bar for non-zero usage.
    if percent > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def severity(percent: float, shutdown_percent: int) -> str:
    """Return one of 'ok' / 'warn' / 'crit' for colour and icon selection."""
    if percent >= shutdown_percent:
        return "crit"
    if percent >= max(0, shutdown_percent - 15):
        return "warn"
    return "ok"


def status_icon(status: str) -> str:
    return {
        "Running": "🟢",
        "Stopped": "⚫",
        "Starting": "🟡",
        "Stopping": "🟡",
    }.get(status, "🟡")


def status_cn(status: str) -> str:
    return {
        "Running": "运行中",
        "Stopped": "已关机",
        "Starting": "启动中",
        "Stopping": "关机中",
        "Unknown": "未知",
    }.get(status, status)


def month_reset_info(now: datetime) -> Tuple[datetime, int]:
    """Return (next reset datetime, whole days remaining) for the billing cycle."""
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return nxt, max(0, (nxt - now).days)


def burn_forecast(
    used_bytes: int, total_bytes: int, shutdown_percent: int, now: datetime
) -> Tuple[float, Optional[float]]:
    """Return (daily average bytes, days until the shutdown threshold).

    Days is None when the pace cannot be estimated yet (first hours of a
    month) or when the projection lands beyond the billing-cycle reset,
    i.e. no risk this cycle. 0.0 means the threshold is already reached.
    """
    elapsed_days = (now.day - 1) + now.hour / 24 + now.minute / 1440
    if elapsed_days < 0.25 or used_bytes <= 0 or total_bytes <= 0:
        return 0.0, None
    daily = used_bytes / elapsed_days
    threshold_bytes = total_bytes * shutdown_percent / 100.0
    if used_bytes >= threshold_bytes:
        return daily, 0.0
    if daily <= 0:
        return daily, None
    days = (threshold_bytes - used_bytes) / daily
    nxt, _ = month_reset_info(now)
    if days > (nxt - now).total_seconds() / 86400:
        return daily, None
    return daily, days


def human_age(seconds: float) -> str:
    if seconds < 0:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def atomic_write_json(path: Path, data: Dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise ConfigError(f"文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取 JSON 文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"JSON 顶层必须是对象: {path}")
    return data


def default_config() -> Dict[str, Any]:
    return {
        "telegram": {"bot_token": "", "admin_user_ids": [], "notify_chat_id": 0},
        "monitor": {
            "interval_seconds": 300,
            "warning_percentages": [80, 90, 95],
            "daily_report_time": "09:00",
            "timezone": "Asia/Taipei",
            "error_notify_cooldown_seconds": 3600,
            "resume_below_percent": 10,
            "max_concurrency": 5,
            "telegram_page_size": 6,
        },
        "instances": [],
    }


def instance_defaults(inst: Dict[str, Any]) -> Dict[str, Any]:
    inst.setdefault("enabled", True)
    inst.setdefault("auto_shutdown", True)
    inst.setdefault("shutdown_percent", 95)
    inst.setdefault("auto_start_next_month", False)
    inst.setdefault("allow_manual_control", True)
    if inst.get("provider") == "ecs_cdt":
        inst.setdefault("traffic_scope", "overseas")
        # Coerce, don't just default: a legacy 2.x config may still carry
        # StopCharging, which reclaims the fixed public IP on stop.
        inst["stopped_mode"] = "KeepCharging"
    return inst


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data = read_json(path)
        self.validate()

    def validate(self) -> None:
        telegram = self.data.get("telegram")
        if not isinstance(telegram, dict):
            raise ConfigError("缺少 telegram 配置")
        if not str(telegram.get("bot_token", "")).strip():
            raise ConfigError("telegram.bot_token 未配置")
        admin_ids = telegram.get("admin_user_ids", [])
        if not isinstance(admin_ids, list) or not admin_ids:
            raise ConfigError("telegram.admin_user_ids 至少需要一个 Telegram 用户 ID")
        try:
            telegram["admin_user_ids"] = [int(x) for x in admin_ids]
            telegram["notify_chat_id"] = int(
                telegram.get("notify_chat_id") or telegram["admin_user_ids"][0]
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError("Telegram 用户 ID / Chat ID 必须是整数") from exc

        monitor = self.data.setdefault("monitor", {})
        for key, value in default_config()["monitor"].items():
            monitor.setdefault(key, value)
        try:
            monitor["interval_seconds"] = max(60, int(monitor["interval_seconds"]))
            monitor["warning_percentages"] = sorted(
                {int(x) for x in monitor["warning_percentages"] if 1 <= int(x) <= 99}
            )
            monitor["max_concurrency"] = min(20, max(1, int(monitor["max_concurrency"])))
            monitor["telegram_page_size"] = min(15, max(3, int(monitor["telegram_page_size"])))
            monitor["resume_below_percent"] = min(50, max(0, int(monitor["resume_below_percent"])))
            ZoneInfo(str(monitor["timezone"]))
        except Exception as exc:
            raise ConfigError(f"monitor 配置无效: {exc}") from exc

        instances = self.data.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ConfigError("instances 至少需要配置一台实例")
        seen: set[str] = set()
        for inst in instances:
            if not isinstance(inst, dict):
                raise ConfigError("instances 中的每个项目必须是对象")
            key = str(inst.get("id", "")).strip()
            if not ID_RE.fullmatch(key):
                raise ConfigError(f"实例 id 仅允许 1-24 位字母、数字、下划线或短横线: {key!r}")
            if key in seen:
                raise ConfigError(f"实例 id 重复: {key}")
            seen.add(key)
            provider = str(inst.get("provider", "ecs_cdt")).lower()
            if provider not in PROVIDERS:
                raise ConfigError(f"{key}: provider 必须为 swas 或 ecs_cdt")
            inst["provider"] = provider
            for f in ("name", "region", "instance_id", "access_key_id", "access_key_secret"):
                if not str(inst.get(f, "")).strip():
                    raise ConfigError(f"{key}: 缺少 {f}")
            instance_defaults(inst)
            try:
                inst["shutdown_percent"] = min(100, max(1, int(inst["shutdown_percent"])))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key}: shutdown_percent 必须是 1-100") from exc
            if provider == "ecs_cdt":
                try:
                    quota = float(inst.get("quota_gb", 0))
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"{key}: ecs_cdt 必须配置 quota_gb") from exc
                if quota <= 0:
                    raise ConfigError(f"{key}: ecs_cdt 的 quota_gb 必须大于 0")
                inst["quota_gb"] = quota
                if inst["traffic_scope"] not in SCOPES:
                    raise ConfigError(f"{key}: traffic_scope 无效")

    @property
    def telegram(self) -> Dict[str, Any]:
        return self.data["telegram"]

    @property
    def monitor(self) -> Dict[str, Any]:
        return self.data["monitor"]

    @property
    def instances(self) -> List[Dict[str, Any]]:
        return self.data["instances"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(str(self.monitor["timezone"]))

    def get_instance(self, key: str) -> Optional[Dict[str, Any]]:
        return next((x for x in self.instances if x["id"] == key), None)

    def save(self) -> None:
        self.validate()
        atomic_write_json(self.path, self.data, 0o600)


class StateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        blank = {"month": "", "instances": {}, "last_daily_report": ""}
        try:
            self.data = read_json(path, dict(blank))
        except ConfigError:
            self.data = dict(blank)
        for key, value in blank.items():
            self.data.setdefault(key, value)

    def instance(self, key: str) -> Dict[str, Any]:
        return self.data["instances"].setdefault(
            key,
            {
                "warned_levels": [],
                "shutdown_triggered": False,
                "pending_auto_start": False,
                "last_error_notify": 0,
                "last_snapshot": {},
            },
        )

    def save(self) -> None:
        atomic_write_json(self.path, self.data, 0o600)


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------


@dataclass
class UsageSnapshot:
    instance_key: str
    name: str
    provider: str
    status: str
    used_bytes: int
    total_bytes: int
    remaining_bytes: int
    overflow_bytes: int = 0
    scope_note: str = ""
    checked_at: float = 0.0
    error: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes * 100.0

    def to_state(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "used_bytes": self.used_bytes,
            "total_bytes": self.total_bytes,
            "remaining_bytes": self.remaining_bytes,
            "overflow_bytes": self.overflow_bytes,
            "percent": round(self.percent, 4),
            "scope_note": self.scope_note,
            "checked_at": self.checked_at,
            "error": self.error,
        }

    @classmethod
    def from_state(cls, key: str, inst: Dict[str, Any], raw: Dict[str, Any]) -> Optional["UsageSnapshot"]:
        """Rebuild a snapshot persisted by a previous run, so a restarted bot
        shows real numbers immediately instead of '尚未查询'."""
        if not raw or not raw.get("checked_at"):
            return None
        return cls(
            instance_key=key,
            name=str(inst.get("name", key)),
            provider=str(inst.get("provider", "ecs_cdt")),
            status=str(raw.get("status", "Unknown")),
            used_bytes=int(raw.get("used_bytes", 0) or 0),
            total_bytes=int(raw.get("total_bytes", 0) or 0),
            remaining_bytes=int(raw.get("remaining_bytes", 0) or 0),
            overflow_bytes=int(raw.get("overflow_bytes", 0) or 0),
            scope_note=str(raw.get("scope_note", "")),
            checked_at=float(raw.get("checked_at", 0) or 0),
            error=str(raw.get("error", "")),
        )


# --------------------------------------------------------------------------
# Aliyun API
# --------------------------------------------------------------------------


class AliyunClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @staticmethod
    def _common_request(
        client,
        domain: str,
        version: str,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        from aliyunsdkcore.request import CommonRequest

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                req = CommonRequest()
                req.set_accept_format("json")
                req.set_protocol_type("https")
                req.set_method("POST")
                req.set_domain(domain)
                req.set_version(version)
                req.set_action_name(action)
                req.set_connect_timeout(5000)
                req.set_read_timeout(15000)
                for key, value in (params or {}).items():
                    req.add_query_param(key, value)
                raw = client.do_action_with_exception(req)
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise AliyunAPIError(f"{action} 返回格式异常")
                return result
            except Exception as exc:  # the SDK raises several unrelated classes
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(1.5 * (2 ** attempt))
        raise AliyunAPIError(f"{action} 调用失败: {last_error}") from last_error

    def _client(self, region: Optional[str] = None):
        from aliyunsdkcore.client import AcsClient

        return AcsClient(
            self.config["access_key_id"],
            self.config["access_key_secret"],
            region or self.config["region"],
        )

    @staticmethod
    def is_mainland(region: str) -> bool:
        """Aliyun's CDT free tier splits the pool into mainland and non-mainland.
        Hong Kong bills against the non-mainland (200 GB) pool."""
        return region.startswith("cn-") and region != "cn-hongkong"

    def get_snapshot(self) -> UsageSnapshot:
        if self.config["provider"] == "swas":
            return self._get_swas_snapshot()
        return self._get_ecs_cdt_snapshot()

    def _get_swas_snapshot(self) -> UsageSnapshot:
        region = self.config["region"]
        instance_id = self.config["instance_id"]
        client = self._client(region)
        domain = f"swas.{region}.aliyuncs.com"
        traffic = self._common_request(
            client,
            domain,
            "2020-06-01",
            "ListInstancesTrafficPackages",
            {"RegionId": region, "InstanceIds": json.dumps([instance_id])},
        )
        usages = traffic.get("InstanceTrafficPackageUsages", [])
        item = next((x for x in usages if x.get("InstanceId") == instance_id), None)
        if not item:
            raise AliyunAPIError("未在 ListInstancesTrafficPackages 返回中找到该实例")

        status_data = self._common_request(
            client,
            domain,
            "2020-06-01",
            "ListInstanceStatus",
            {
                "RegionId": region,
                "InstanceIds": json.dumps([instance_id]),
                "PageNumber": 1,
                "PageSize": 10,
            },
        )
        statuses = status_data.get("InstanceStatuses", [])
        status_item = next((x for x in statuses if x.get("InstanceId") == instance_id), None)
        status = (status_item or {}).get("Status", "Unknown")
        total = int(item.get("TrafficPackageTotal", 0) or 0)
        used = int(item.get("TrafficUsed", 0) or 0)
        remaining = int(item.get("TrafficPackageRemaining", max(total - used, 0)) or 0)
        overflow = int(item.get("TrafficOverflow", 0) or 0)
        return UsageSnapshot(
            instance_key=self.config["id"],
            name=self.config["name"],
            provider="swas",
            status=status,
            used_bytes=used,
            total_bytes=total,
            remaining_bytes=remaining,
            overflow_bytes=overflow,
            scope_note="轻量应用服务器实例级出网流量包",
            checked_at=time.time(),
        )

    def _get_ecs_cdt_snapshot(self) -> UsageSnapshot:
        region = self.config["region"]
        instance_id = self.config["instance_id"]
        # CDT is a global service; cn-hangzhou is the reliable endpoint region.
        cdt_client = self._client("cn-hangzhou")
        traffic_data = self._common_request(
            cdt_client, "cdt.aliyuncs.com", "2021-08-13", "ListCdtInternetTraffic"
        )
        scope = self.config.get("traffic_scope", "overseas")
        used = 0
        for detail in traffic_data.get("TrafficDetails", []):
            business_region = str(detail.get("BusinessRegionId", ""))
            if scope == "all":
                include = True
            elif scope == "mainland":
                include = self.is_mainland(business_region)
            elif scope == "overseas":
                include = not self.is_mainland(business_region)
            else:  # exact_region
                include = business_region == region
            if include:
                used += int(detail.get("Traffic", 0) or 0)

        status = self.get_status()
        total = int(float(self.config["quota_gb"]) * GIB)
        note_map = {
            "all": "CDT 账号全部公网累计用量",
            "mainland": "CDT 账号中国内地累计用量",
            "overseas": "CDT 账号非中国内地累计用量",
            "exact_region": f"CDT 账号 {region} 地域累计用量",
        }
        return UsageSnapshot(
            instance_key=self.config["id"],
            name=self.config["name"],
            provider="ecs_cdt",
            status=status,
            used_bytes=used,
            total_bytes=total,
            remaining_bytes=max(total - used, 0),
            overflow_bytes=max(used - total, 0),
            scope_note=note_map[scope] + "（账号池口径，非实例网卡计量）",
            checked_at=time.time(),
        )

    def get_status(self) -> str:
        """Instance power state, used both by snapshots and by validation."""
        region = self.config["region"]
        instance_id = self.config["instance_id"]
        client = self._client(region)
        if self.config["provider"] == "swas":
            data = self._common_request(
                client,
                f"swas.{region}.aliyuncs.com",
                "2020-06-01",
                "ListInstanceStatus",
                {
                    "RegionId": region,
                    "InstanceIds": json.dumps([instance_id]),
                    "PageNumber": 1,
                    "PageSize": 10,
                },
            )
            item = next(
                (x for x in data.get("InstanceStatuses", []) if x.get("InstanceId") == instance_id),
                None,
            )
            if item is None:
                raise AliyunAPIError(f"该地域下未找到实例 {instance_id}")
            return str(item.get("Status", "Unknown"))

        data = self._common_request(
            client,
            f"ecs.{region}.aliyuncs.com",
            "2014-05-26",
            "DescribeInstances",
            {"RegionId": region, "InstanceIds": json.dumps([instance_id])},
        )
        instances = data.get("Instances", {}).get("Instance", [])
        if not instances:
            raise AliyunAPIError(f"该地域下未找到实例 {instance_id}")
        return str(instances[0].get("Status", "Unknown"))

    def control(self, action: str) -> None:
        provider = self.config["provider"]
        region = self.config["region"]
        instance_id = self.config["instance_id"]
        client = self._client(region)
        action_map = {"start": "StartInstance", "stop": "StopInstance", "reboot": "RebootInstance"}
        if action not in action_map:
            raise AliyunAPIError(f"不支持的操作: {action}")

        if provider == "swas":
            self._common_request(
                client,
                f"swas.{region}.aliyuncs.com",
                "2020-06-01",
                action_map[action],
                {"RegionId": region, "InstanceId": instance_id, "ClientToken": str(uuid.uuid4())},
            )
            return

        params: Dict[str, Any] = {"RegionId": region, "InstanceId": instance_id}
        if action == "stop":
            # Hard-coded KeepCharging: StopCharging (节省停机) reclaims the
            # instance's fixed public IP, which must never happen here. This
            # is deliberately not configurable.
            params["StoppedMode"] = "KeepCharging"
        self._common_request(client, f"ecs.{region}.aliyuncs.com", "2014-05-26", action_map[action], params)


# --------------------------------------------------------------------------
# Live validation, used by the terminal panel right after credentials are typed
# --------------------------------------------------------------------------


@dataclass
class ValidationResult:
    okay: bool
    status: str = ""
    used_bytes: int = 0
    total_bytes: int = 0
    messages: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes * 100.0


def validate_instance_live(inst: Dict[str, Any]) -> ValidationResult:
    """Call the real Aliyun API with the credentials just entered.

    Traffic and power state are probed separately so that a RAM policy missing
    only one of the two permissions produces a precise message instead of a
    generic failure.
    """
    client = AliyunClient(instance_defaults(dict(inst)))
    messages: List[str] = []

    try:
        status = client.get_status()
        messages.append(f"实例状态读取成功：{status_cn(status)}")
    except Exception as exc:
        return ValidationResult(
            okay=False,
            error=str(exc),
            messages=["实例状态读取失败 —— 检查 AccessKey、Region、Instance ID，以及 ecs:DescribeInstances 权限"],
        )

    try:
        snap = client.get_snapshot()
    except Exception as exc:
        return ValidationResult(
            okay=False,
            status=status,
            error=str(exc),
            messages=messages + ["流量读取失败 —— 检查 cdt:ListCdtInternetTraffic 权限，或该账号是否已开通 CDT"],
        )

    messages.append(f"流量读取成功：{fmt_gb(snap.used_bytes)} / {fmt_gb(snap.total_bytes)}")
    if inst.get("provider") == "ecs_cdt" and snap.used_bytes == 0:
        messages.append("当前 CDT 用量为 0，若这台机器已经跑了一段时间，请确认流量口径选择是否正确")
    return ValidationResult(
        okay=True,
        status=status,
        used_bytes=snap.used_bytes,
        total_bytes=snap.total_bytes,
        messages=messages,
    )
