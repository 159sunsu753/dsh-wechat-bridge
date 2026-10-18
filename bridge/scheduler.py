# -*- coding: utf-8 -*-
"""定时打单任务（bridge 直接 import 的稳定 API）。

对外（DEV-INTERFACES §4）::

    add_from_text(text, meta) -> dict | None
    due_jobs(now_ts=None) -> list[dict]
    mark(job_id, status, result=None) -> bool
    list_jobs() -> list[dict]

补充：``parse_schedule(text, now_ts)`` 只解析不落盘；``get_job`` / ``recover_stale``
给桥接重启后恢复用。

支持写法（示例）::

    定时 2026-10-19 10:00 打 TEST-0005 40HQ 提柜:蛇口
    明天 10:00 打 TEST-0005
    今天 16:30 打 CHN4105078EXX
    定时打 TEST-0005 到 2026-10-19 10:00
    定时 2026-10-19 10:00 打 TEST-0005（也支持 10点 / 10点30 / 下午4点）

持久化：``工具\\wechat-bridge\\state\\scheduled.json``，临时文件 + ``os.replace`` 原子写，
跨重启保留；``due_jobs`` 认领任务时把状态改成 ``running``，所以同一任务只返回一次。
状态：pending / running / done / failed；失败带 attempts 上限（默认 3 次，重试间隔递增）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("wechat.scheduler")

_HERE = Path(__file__).resolve().parent

#: 任务持久化文件；测试可赋值为临时路径。
STATE_FILE = Path(
    os.environ.get("DSH_WECHAT_SCHEDULER_STATE") or str(_HERE / "state" / "scheduled.json")
)

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 60.0
STALE_RUNNING_SEC = 900.0
PAST_TOLERANCE_SEC = 60.0
VALID_STATUS = ("pending", "running", "done", "failed")

_LOCK = threading.RLock()

_FULLWIDTH = str.maketrans("０１２３４５６７８９：－／　", "0123456789:-/ ")
_DAY_WORDS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "后日": 2}
_PERIODS = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上")

_ISO_RE = re.compile(
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<mo>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*(?:日|号)?"
    r"\s*(?:T|\s)*"
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<h>\d{1,2})\s*(?P<sep>[:：点时])\s*(?:(?P<mi>\d{1,2})\s*分?|(?P<half>半))?"
)

_DAY_RE = re.compile(
    r"(?P<day>今天|今日|明天|明日|后天|后日)\s*"
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<h>\d{1,2})\s*(?P<sep>[:：点时])\s*(?:(?P<mi>\d{1,2})\s*分?|(?P<half>半))?"
)

_BARE_RE = re.compile(
    r"(?<!\d)(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<h>\d{1,2})\s*(?P<sep>[:：点时])\s*(?:(?P<mi>\d{1,2})\s*分?|(?P<half>半))?(?!\d)"
)

_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9-]{4,24})(?![A-Za-z0-9])")
_BOX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<num>20|40|45|48|50|53)\s*(?P<type>GP|HQ|HC|RF|OT|FR|NOR|TK|DV|DC|RH)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BOX_TOKEN_RE = re.compile(
    r"^(?:20|40|45|48|50|53)(?:GP|HQ|HC|RF|OT|FR|NOR|TK|DV|DC|RH)$", re.IGNORECASE
)
_PICKUP_RE = re.compile(r"(?:提柜点|提柜地点|提柜|还柜点|还柜)\s*[:：]?\s*([^\s,，。;；|、]+)")
_PICKUP_PREFIX_RE = re.compile(r"^([\u4e00-\u9fa5]{2,8})\s*(?:提柜|还柜)")
_SCHEDULE_WORDS = ("定时", "预约", "提醒")
_ACTION_RE = re.compile(r"打单|打印|打|办单|办|约柜|约|查单|查|下载|提醒")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _stamp(ts: Optional[float] = None) -> str:
    try:
        return time.strftime("%Y%m%d_%H%M%S", time.localtime(_safe_float(ts, _now())))
    except (ValueError, OSError, OverflowError):
        return time.strftime("%Y%m%d_%H%M%S")


def _normalize_text(text: Any) -> str:
    value = str(text if text is not None else "")
    value = value.translate(_FULLWIDTH)
    value = (
        value.replace("今晚", "今天 晚上")
        .replace("明晚", "明天 晚上")
        .replace("今早", "今天 早上")
        .replace("明早", "明天 早上")
    )
    return value


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _apply_period(hour: int, period: Optional[str]) -> int:
    if not period:
        return hour
    if period in ("下午", "傍晚", "晚上"):
        return hour + 12 if hour < 12 else hour
    if period == "中午":
        return hour if hour >= 11 else hour + 12
    if period in ("凌晨", "早上"):
        return 0 if hour == 12 else hour
    return hour


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _match_time(work: str) -> Tuple[Optional[re.Match], Optional[str]]:
    match = _ISO_RE.search(work)
    if match:
        return match, "absolute"
    match = _DAY_RE.search(work)
    if match:
        return match, "relative"
    if any(word in work for word in _SCHEDULE_WORDS):
        match = _BARE_RE.search(work)
        if match:
            return match, "bare"
    return None, None


def _time_from_match(match: re.Match, kind: str, now_dt: datetime) -> Optional[datetime]:
    try:
        hour = _apply_period(int(match.group("h")), match.groupdict().get("period"))
        minute_raw = match.group("mi")
        if minute_raw:
            minute = int(minute_raw)
        elif match.groupdict().get("half"):
            minute = 30
        else:
            minute = 0
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if kind == "absolute":
        try:
            year = int(match.group("y"))
            month = int(match.group("mo"))
            day = int(match.group("d"))
            return datetime(year, month, day, hour, minute)
        except (TypeError, ValueError, OverflowError):
            return None
    offset = 0
    if kind == "relative":
        offset = _DAY_WORDS.get(match.group("day") or "", 0)
    try:
        base = now_dt.date() + timedelta(days=offset)
        return datetime(base.year, base.month, base.day, hour, minute)
    except (ValueError, OverflowError):
        return None


def _strip_schedule_words(instruction: str) -> str:
    text = _squash(instruction)
    for _ in range(3):
        before = text
        text = re.sub(r"^(?:定时|预约|提醒我|提醒|帮我|请|到时候)\s*", "", text)
        text = re.sub(r"\s*[到在于]\s*$", "", text)
        text = re.sub(r"^[，,。.;；:：\-\s]+", "", text)
        if text == before:
            break
    return text.strip(" ，,。.;；!！")


def _extract_so(text: str) -> Optional[str]:
    for match in _TOKEN_RE.finditer(str(text or "")):
        token = match.group(1).strip("-_")
        if len(token) < 5:
            continue
        upper = token.upper()
        if _BOX_TOKEN_RE.match(upper):
            continue
        compact = re.sub(r"[^0-9A-Za-z]", "", upper)
        if not compact or not re.search(r"\d", compact):
            continue
        has_letter = bool(re.search(r"[A-Z]", compact))
        if not has_letter and len(compact) < 8:
            continue
        return token
    return None


def _extract_box(text: str) -> Optional[str]:
    match = _BOX_RE.search(str(text or ""))
    if not match:
        return None
    return "{}{}".format(match.group("num"), match.group("type").upper())


def _extract_pickup(text: str) -> Optional[str]:
    text = str(text or "")
    match = _PICKUP_RE.search(text)
    if match:
        value = match.group(1).strip()
        value = re.sub(r"^(?:在|到|于)", "", value)
        if value:
            return value
    match = _PICKUP_PREFIX_RE.search(text)
    if match:
        return match.group(1)
    return None


def parse_schedule(text: str, now_ts: Optional[float] = None) -> Optional[dict]:
    """解析一条定时文本，返回 job 核心字段；不落盘。解析不出/时间已过返回 None。"""
    work = _normalize_text(text)
    if not work.strip():
        return None
    now = _now() if now_ts is None else float(now_ts)
    now_dt = datetime.fromtimestamp(now)
    match, kind = _match_time(work)
    if match is None or kind is None:
        return None
    due_dt = _time_from_match(match, kind, now_dt)
    if due_dt is None:
        return None
    due_ts = due_dt.timestamp()
    if due_ts < now - PAST_TOLERANCE_SEC:
        return None

    remainder = (work[: match.start()] + " " + work[match.end():]).strip()
    instruction = _strip_schedule_words(remainder)
    if not instruction:
        instruction = _strip_schedule_words(work[: match.start()]) or _squash(work)
    so = _extract_so(instruction) or _extract_so(work)
    box = _extract_box(instruction) or _extract_box(work)
    pickup = _extract_pickup(instruction) or _extract_pickup(work)

    if "约柜" in instruction:
        action = "约柜"
    elif "打" in instruction or "打印" in instruction:
        action = "打单"
    elif "办" in instruction:
        action = "办单"
    elif "查" in instruction:
        action = "查单"
    else:
        action = "提醒"

    return {
        "due_ts": round(due_ts, 3),
        "due_text": due_dt.strftime("%Y-%m-%d %H:%M"),
        "time_expr": _squash(match.group(0)),
        "so": so,
        "box": box,
        "pickup": pickup,
        "action": action,
        "text": instruction,
        "pattern": kind,
    }


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return Path(str(STATE_FILE)).expanduser()


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "jobs": []}


def _load() -> Dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _empty_state()
    try:
        with open(str(path), "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        LOGGER.warning("scheduled.json 读取失败，改名留档后重建: %s", exc)
        try:
            path.replace(path.with_name(path.name + ".corrupt-" + _stamp()))
        except OSError:
            pass
        return _empty_state()

    if isinstance(data, list):
        data = {"version": 1, "jobs": data}
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return _empty_state()
    jobs = [job for job in data["jobs"] if isinstance(job, dict) and job.get("id")]
    data["jobs"] = jobs
    return data


def _save(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    last: Optional[BaseException] = None
    for attempt in range(6):
        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".scheduled-", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, str(path))
            return
        except (PermissionError, OSError) as exc:
            last = exc
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if attempt < 5:
                time.sleep(min(0.12 * (attempt + 1), 1.0))
    assert last is not None
    raise last


def _safe_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in (meta or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[str(key)] = value if not isinstance(value, str) else value[:500]
        elif isinstance(value, dict):
            clean[str(key)] = {
                str(k): (v if not isinstance(v, str) else v[:200])
                for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
    return clean


def _new_id(now: float) -> str:
    try:
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d%H%M%S")
    except (ValueError, OSError, OverflowError):
        stamp = _stamp(now)
    return "sch-{}-{}".format(stamp, uuid.uuid4().hex[:6])


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def add_from_text(text: str, meta: Optional[dict] = None) -> Optional[dict]:
    """识别定时文本并落盘；不是定时任务（或时间已过）返回 None。

    meta 可含 ``now_ts``（测试/回放用）、``sender``、``source``、``chat``。
    同一文本 + 同一到期时间重复添加时返回已有 pending/running 任务（幂等）。
    """
    meta = dict(meta or {})
    now = _safe_float(meta.get("now_ts"), _now())
    parsed = parse_schedule(text, now_ts=now)
    if not parsed:
        return None
    job: Dict[str, Any] = {
        "id": _new_id(now),
        "due_ts": parsed["due_ts"],
        "due_text": parsed["due_text"],
        "time_expr": parsed["time_expr"],
        "so": parsed["so"],
        "box": parsed["box"],
        "pickup": parsed["pickup"],
        "action": parsed["action"],
        "text": parsed["text"],
        "raw": _squash(str(text or "")),
        "pattern": parsed["pattern"],
        "created_ts": round(now, 3),
        "status": "pending",
        "attempts": 0,
        "source": meta.get("source") or meta.get("chat") or meta.get("group"),
        "sender": meta.get("sender"),
        "meta": _safe_meta(meta),
    }
    with _LOCK:
        state = _load()
        for existing in state["jobs"]:
            if existing.get("status") not in ("pending", "running"):
                continue
            if existing.get("raw") != job["raw"]:
                continue
            if abs(_safe_float(existing.get("due_ts")) - job["due_ts"]) < 1.0:
                return copy.deepcopy(existing)
        state["jobs"].append(job)
        _save(state)
    return copy.deepcopy(job)


def _recover_stale(state: Dict[str, Any], now: float) -> bool:
    """进程崩溃后 running 任务卡死：超过 STALE_RUNNING_SEC 且有重试额度则回 pending。"""
    changed = False
    for job in state["jobs"]:
        if job.get("status") != "running":
            continue
        started = _safe_float(job.get("started_ts") or job.get("last_attempt_ts"))
        if not started or now - started < STALE_RUNNING_SEC:
            continue
        attempts = int(_safe_float(job.get("attempts")))
        if attempts >= MAX_ATTEMPTS:
            job["status"] = "failed"
            job["finished_ts"] = round(now, 3)
            job["result"] = {"error": "stale_running"}
        else:
            job["status"] = "pending"
            job["retry_after_ts"] = 0
            job["stale_recovered_ts"] = round(now, 3)
        job["updated_ts"] = round(now, 3)
        changed = True
    return changed


def due_jobs(now_ts: Optional[float] = None) -> List[dict]:
    """返回此刻到期的任务，并把它们认领为 running（同一任务只返回一次）。"""
    now = _now() if now_ts is None else float(now_ts)
    with _LOCK:
        state = _load()
        changed = _recover_stale(state, now)
        due: List[dict] = []
        for job in state["jobs"]:
            if job.get("status") != "pending":
                continue
            due_ts = _safe_float(job.get("due_ts"), float("inf"))
            if due_ts > now:
                continue
            retry_after = _safe_float(job.get("retry_after_ts"))
            if retry_after > now:
                continue
            attempts = int(_safe_float(job.get("attempts")))
            if attempts >= MAX_ATTEMPTS:
                job["status"] = "failed"
                job["finished_ts"] = round(now, 3)
                job["result"] = {"error": "max_attempts"}
                job["updated_ts"] = round(now, 3)
                changed = True
                continue
            job["status"] = "running"
            job["attempts"] = attempts + 1
            job["started_ts"] = round(now, 3)
            job["last_attempt_ts"] = round(now, 3)
            job["updated_ts"] = round(now, 3)
            changed = True
            due.append(copy.deepcopy(job))
        if changed:
            _save(state)
    return due


def mark(job_id: str, status: str, result: Optional[dict] = None) -> bool:
    """更新任务状态。

    - ``done``：完成；
    - ``failed``：attempts 未达上限则自动回到 pending（延迟重试），达到上限才终态 failed；
    - ``running`` / ``pending``：显式改状态（人工接管/重排）。
    返回是否找到并更新。
    """
    status = str(status or "").strip()
    if status not in VALID_STATUS:
        raise ValueError("非法状态: {!r}（允许 {}）".format(status, "/".join(VALID_STATUS)))
    now = _now()
    found = False
    with _LOCK:
        state = _load()
        for job in state["jobs"]:
            if job.get("id") != job_id:
                continue
            job["status"] = status
            job["updated_ts"] = round(now, 3)
            if result is not None:
                job["result"] = result
            if status == "done":
                job["finished_ts"] = round(now, 3)
                job.pop("retry_after_ts", None)
            elif status == "failed":
                attempts = int(_safe_float(job.get("attempts")))
                job["last_error"] = result if result is not None else job.get("result")
                if attempts >= MAX_ATTEMPTS:
                    job["status"] = "failed"
                    job["finished_ts"] = round(now, 3)
                else:
                    job["status"] = "pending"
                    job["retry_after_ts"] = round(now + RETRY_DELAY_SEC * max(1, attempts), 3)
            elif status == "running":
                job.setdefault("started_ts", round(now, 3))
            elif status == "pending":
                job.pop("finished_ts", None)
            found = True
            break
        if found:
            _save(state)
    return found


def list_jobs(status: Optional[str] = None) -> List[dict]:
    """列出任务（按到期时间升序）；status 可选过滤。"""
    with _LOCK:
        state = _load()
    jobs = [copy.deepcopy(job) for job in state["jobs"]]
    if status:
        jobs = [job for job in jobs if job.get("status") == status]
    jobs.sort(key=lambda job: (_safe_float(job.get("due_ts")), str(job.get("id"))))
    return jobs


def get_job(job_id: str) -> Optional[dict]:
    """按 id 取单个任务（桥接重启后对账用）。"""
    with _LOCK:
        state = _load()
    for job in state["jobs"]:
        if job.get("id") == job_id:
            return copy.deepcopy(job)
    return None


def recover_stale(now_ts: Optional[float] = None) -> int:
    """手动触发一次 running 卡死恢复，返回改动条数。"""
    now = _now() if now_ts is None else float(now_ts)
    with _LOCK:
        state = _load()
        changed = _recover_stale(state, now)
        if changed:
            _save(state)
    return 1 if changed else 0


__all__ = [
    "STATE_FILE",
    "MAX_ATTEMPTS",
    "RETRY_DELAY_SEC",
    "VALID_STATUS",
    "parse_schedule",
    "add_from_text",
    "due_jobs",
    "mark",
    "list_jobs",
    "get_job",
    "recover_stale",
]
