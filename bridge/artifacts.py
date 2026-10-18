# -*- coding: utf-8 -*-
"""桌面WeChatBridgeData产物管理（bridge 直接 import 的稳定 API）。

目录契约（DEV-INTERFACES §2/§3）::

    C:\\Users\\Public\\Desktop\\WeChatBridgeData\\
      微信下载图片\\ 微信下载文件\\ 微信长文本\\ 打单文件\\ EIR单\\
      打单反馈截图\\{约柜,无纸化,无资料,异常,其他}\\

对外 API::

    BASE
    ensure_layout() -> dict
    save_inbound_image(src_path, meta) -> str
    save_inbound_file(src_path, meta) -> str
    save_long_text(text, meta) -> str
    parse_bridge_block(reply_text) -> (clean_text, dict)
    select_attachments(reply_text, bridge_json, context) -> list[dict]
    organize_recent(since_ts, context) -> list[str]
    classify_feedback(name, context=None) -> str | None

实现约定：
- 只写 BASE（或 context["base"]）下的子目录；任何情况都不删除文件。
- 重名不覆盖：``<名字>_<YYYYMMDD_HHMMSS>``，仍冲突再追加 ``_2``、``_3``。
- 中文路径统一 pathlib；Windows 文件锁用短重试。
- 审计写 ``BASE/_index.jsonl``（JSON Lines，追加，失败不抛）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

LOGGER = logging.getLogger("wechat.artifacts")

#: 产物根目录。bridge 可用 config["paths"]["base"] 覆盖（直接赋值 artifacts.BASE），
#: 测试/临时目录也可用环境变量 DSH_WECHAT_ARTIFACTS_BASE 覆盖。
BASE = os.environ.get("DSH_WECHAT_ARTIFACTS_BASE") or r"C:\Users\Public\Desktop\WeChatBridgeData"

DIR_IMAGE = "微信下载图片"
DIR_FILE = "微信下载文件"
DIR_TEXT = "微信长文本"
DIR_PRINT = "打单文件"
DIR_EIR = "EIR单"
DIR_FEEDBACK = "打单反馈截图"
LEGACY_EVIDENCE = "证据"

FEEDBACK_CATEGORIES: Tuple[str, ...] = (
    "约柜", "无纸化", "无纸化-鹏海运", "无纸化-联运通", "无资料", "异常", "其他"
)

MARKER = "<<<BRIDGE>>>"
INDEX_FILE = "_index.jsonl"
LONG_TEXT_THRESHOLD = 300
ATTACH_MAX = 2
DISCOVER_WINDOW_SEC = 900.0
CLOCK_SLACK_SEC = 2.0

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
TEXT_SUFFIXES = {".txt", ".md", ".json", ".csv", ".log"}

_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"RIFF",
    b"II*\x00",
    b"MM\x00*",
)

_KNOWN_CATEGORIES = (
    "eir",
    "print",
    "wechat_image",
    "wechat_file",
    "long_text",
) + FEEDBACK_CATEGORIES

_WIN_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SPACES = re.compile(r"\s+")

# 附件/证据文件名里的分类关键词（命中即归类，按顺序优先）
_FEEDBACK_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("约柜", ("约柜",)),
    ("无纸化", ("无纸化", "纸化")),
    ("无资料", ("无资料", "查无", "无数据", "无信息", "未查到", "无柜")),
    ("异常", ("异常", "失败", "不符", "错误", "超时", "拒绝", "无法", "异提", "超限", "报错")),
    ("其他", ("打单后", "打单完成", "成功", "已确认", "已办单", "完成", "已缴费", "缴费", "支付", "办单")),
)

_EIR_HINTS = ("eir", "设备交接", "交接单", "授权码")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _resolve_base(context: Optional[dict] = None) -> Path:
    """返回本次调用使用的根目录（context["base"] 优先，便于测试/多实例）。"""
    custom = (context or {}).get("base") if isinstance(context, dict) else None
    root = custom if custom else BASE
    return Path(str(root)).expanduser()


def _iso(ts: Optional[float] = None) -> str:
    try:
        return datetime.fromtimestamp(float(ts) if ts is not None else time.time()).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _stamp(ts: float) -> str:
    try:
        return time.strftime("%Y%m%d_%H%M%S", time.localtime(float(ts)))
    except (ValueError, OSError, OverflowError):
        return time.strftime("%Y%m%d_%H%M%S")


def _meta_ts(meta: Optional[dict]) -> float:
    """meta 里取事件时间：数值 ts 优先，其次 ISO 字符串，最后 now。"""
    meta = meta or {}
    for key in ("ts", "timestamp", "time_ts", "captured_at_ts", "sent_ts"):
        value = meta.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError:
                pass
    for key in ("captured_at", "datetime", "time"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return time.time()


def _sender(meta: Optional[dict], limit: int = 30) -> str:
    meta = meta or {}
    raw = meta.get("sender") or meta.get("from") or meta.get("nickname") or "未知"
    return _safe_name(raw, limit) or "未知"


def _short_id(meta: Optional[dict]) -> str:
    meta = meta or {}
    basis = meta.get("id") or meta.get("msg_id") or meta.get("request_id") or uuid.uuid4().hex
    return hashlib.sha1(str(basis).encode("utf-8")).hexdigest()[:8]


def _safe_name(value: Any, maxlen: int = 60) -> str:
    """Windows 安全文件名片段（不处理扩展名）。"""
    text = _WIN_BAD_CHARS.sub("_", str(value if value is not None else ""))
    text = _SPACES.sub(" ", text).strip(" .")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip(" .")
    return text


def _safe_filename(original: str, maxlen: int = 120) -> str:
    """保留扩展名的安全文件名（用于转发客户文件）。"""
    name = Path(str(original or "")).name
    stem = Path(name).stem
    suffix = Path(name).suffix
    stem = _safe_name(stem, maxlen)
    if not stem:
        stem = "file"
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        # 奇怪的扩展名直接并入主干，避免路径穿越
        stem = _safe_name(stem + suffix, maxlen)
        suffix = ""
    return stem + suffix


def _unique_path(target: Path, ts: Optional[float] = None) -> Path:
    """重名不覆盖：先加 _<YYYYMMDD_HHMMSS>，仍冲突再加 _2/_3。"""
    target = Path(target)
    if not target.exists():
        return target
    stamp = _stamp(ts if ts is not None else time.time())
    candidate = target.with_name("{}_{}{}".format(target.stem, stamp, target.suffix))
    index = 2
    while candidate.exists():
        candidate = target.with_name("{}_{}_{}{}".format(target.stem, stamp, index, target.suffix))
        index += 1
        if index > 999:
            candidate = target.with_name("{}_{}{}".format(target.stem, uuid.uuid4().hex[:8], target.suffix))
            break
    return candidate


def _sleep_retry(attempt: int, base: float = 0.12) -> None:
    time.sleep(min(base * (attempt + 1), 1.0))


def _copy_retry(src: Path, dst: Path, attempts: int = 6) -> None:
    """copy2 + Windows 文件锁短重试。复制失败时清掉半成品，不抛给发送链。"""
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            shutil.copy2(str(src), str(dst))
            return
        except (PermissionError, OSError) as exc:  # Windows: 目标被占用/短暂锁
            last = exc
            try:
                if dst.exists() and dst.stat().st_size == 0:
                    dst.unlink()
            except OSError:
                pass
            if attempt < attempts - 1:
                _sleep_retry(attempt)
    assert last is not None
    raise last


def _move_retry(src: Path, dst: Path, attempts: int = 6) -> None:
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            shutil.move(str(src), str(dst))
            return
        except (PermissionError, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                _sleep_retry(attempt)
    assert last is not None
    raise last


def _write_text_retry(path: Path, text: str, attempts: int = 6) -> None:
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
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
            if attempt < attempts - 1:
                _sleep_retry(attempt)
    assert last is not None
    raise last


def _mkdir_retry(path: Path, attempts: int = 5) -> Path:
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except (PermissionError, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                _sleep_retry(attempt)
    assert last is not None
    raise last


def _is_within(path: Path, folder: Path) -> bool:
    try:
        left = os.path.normcase(os.path.abspath(str(Path(path))))
        right = os.path.normcase(os.path.abspath(str(Path(folder))))
        return os.path.commonpath([left, right]) == right
    except ValueError:
        return False


def _sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _audit(entry: Optional[dict], base: Optional[Union[str, Path]] = None) -> None:
    """追加审计到 BASE/_index.jsonl；任何异常都吞掉，绝不影响主流程。"""
    try:
        root = Path(str(base)).expanduser() if base else Path(str(BASE)).expanduser()
        _mkdir_retry(root)
        record = {
            "ts": round(time.time(), 3),
            "time": _iso(),
            "module": "artifacts",
        }
        record.update(entry or {})
        line = json.dumps(record, ensure_ascii=False, default=str)
        for attempt in range(5):
            try:
                with open(str(root / INDEX_FILE), "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                return
            except OSError:
                if attempt >= 4:
                    raise
                _sleep_retry(attempt)
    except Exception as exc:  # noqa: BLE001 - 审计失败不能影响主流程
        LOGGER.warning("审计写入失败: %s", exc)


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------


def _layout_map() -> Dict[str, Tuple[str, ...]]:
    return {
        "wechat_image": (DIR_IMAGE,),
        "wechat_file": (DIR_FILE,),
        "long_text": (DIR_TEXT,),
        "print": (DIR_PRINT,),
        "eir": (DIR_EIR,),
        "feedback": (DIR_FEEDBACK,),
        "feedback_约柜": (DIR_FEEDBACK, "约柜"),
        "feedback_无纸化": (DIR_FEEDBACK, "无纸化"),
        "feedback_无纸化_鹏海运": (DIR_FEEDBACK, "无纸化", "鹏海运"),
        "feedback_无纸化_联运通": (DIR_FEEDBACK, "无纸化", "联运通"),
        "feedback_无资料": (DIR_FEEDBACK, "无资料"),
        "feedback_异常": (DIR_FEEDBACK, "异常"),
        "feedback_其他": (DIR_FEEDBACK, "其他"),
    }


def ensure_layout_at(root: Union[str, Path]) -> Dict[str, str]:
    """在 root 下建齐产物目录（幂等），返回 key -> 绝对路径。"""
    base = Path(str(root)).expanduser()
    _mkdir_retry(base)
    result: Dict[str, str] = {"base": str(base.resolve())}
    for key, parts in _layout_map().items():
        folder = _mkdir_retry(base.joinpath(*parts))
        result[key] = str(folder.resolve())
    return result


def ensure_layout() -> Dict[str, str]:
    """建齐 BASE 下的产物目录（幂等），返回 key -> 绝对路径。"""
    return ensure_layout_at(BASE)


def _ensure_category_dir(category: str, root: Path) -> Path:
    parts = _category_parts(category)
    return _mkdir_retry(Path(root).joinpath(*parts))


def _category_parts(category: str) -> Tuple[str, ...]:
    category = str(category or "").strip()
    if category == "eir":
        return (DIR_EIR,)
    if category == "print":
        return (DIR_PRINT,)
    if category in ("无纸化", "无纸化-鹏海运"):
        return (DIR_FEEDBACK, "无纸化", "鹏海运")
    if category == "无纸化-联运通":
        return (DIR_FEEDBACK, "无纸化", "联运通")
    if category in FEEDBACK_CATEGORIES:
        return (DIR_FEEDBACK, category)
    if category == "wechat_image":
        return (DIR_IMAGE,)
    if category == "wechat_file":
        return (DIR_FILE,)
    if category == "long_text":
        return (DIR_TEXT,)
    return (DIR_PRINT,)


# ---------------------------------------------------------------------------
# 入站产物保存
# ---------------------------------------------------------------------------


def save_inbound_image(src_path: str, meta: Optional[dict] = None) -> str:
    """微信入站图片 -> 微信下载图片\\<时间>_<发送人>_<短id>.<ext>。返回绝对路径。"""
    src = Path(str(src_path)).expanduser()
    if not src.is_file():
        raise FileNotFoundError("图片不存在: {}".format(src))
    if not _image_ok(src):
        raise ValueError("不是可解码图片: {}".format(src))
    meta = dict(meta or {})
    ts = _meta_ts(meta)
    suffix = src.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    name = "{}_{}_{}{}".format(_stamp(ts), _sender(meta), _short_id(meta), suffix)
    folder = _ensure_category_dir("wechat_image", _resolve_base())
    dst = _unique_path(folder / name, ts)
    _copy_retry(src, dst)
    return str(dst.resolve())


def save_inbound_file(src_path: str, meta: Optional[dict] = None) -> str:
    """微信入站文件 -> 微信下载文件\\<原文件名>（重名加时间后缀）。返回绝对路径。"""
    src = Path(str(src_path)).expanduser()
    if not src.is_file():
        raise FileNotFoundError("文件不存在: {}".format(src))
    meta = dict(meta or {})
    ts = _meta_ts(meta)
    name = _safe_filename(src.name)
    folder = _ensure_category_dir("wechat_file", _resolve_base())
    dst = _unique_path(folder / name, ts)
    _copy_retry(src, dst)
    return str(dst.resolve())


def save_long_text(text: str, meta: Optional[dict] = None) -> str:
    """长文本（>300 字）-> 微信长文本\\<时间>_<发送人>_<短id>.txt。返回绝对路径。"""
    meta = dict(meta or {})
    ts = _meta_ts(meta)
    body = "" if text is None else str(text)
    name = "{}_{}_{}.txt".format(_stamp(ts), _sender(meta), _short_id(meta))
    folder = _ensure_category_dir("long_text", _resolve_base())
    dst = _unique_path(folder / name, ts)
    header = (
        "[微信长文本]\n"
        "发送人：{}\n"
        "会话：{}\n"
        "消息id：{}\n"
        "时间：{}\n"
        "{}字\n"
        "{}\n"
    ).format(
        _sender(meta, 80),
        _safe_name(meta.get("chat") or meta.get("group") or "", 60) or "-",
        meta.get("id") or meta.get("msg_id") or "-",
        _iso(ts),
        len(body),
        "-" * 32,
    )
    _write_text_retry(dst, header + body + ("\n" if not body.endswith("\n") else ""))
    return str(dst.resolve())


# ---------------------------------------------------------------------------
# <<<BRIDGE>>> 解析
# ---------------------------------------------------------------------------


def _decode_json_candidate(text: str) -> Optional[Any]:
    """尽量从一段文本里解出 JSON：整段优先，其次从第一个 {/[ 开始 raw_decode。"""
    candidate = (text or "").strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        pass
    start = -1
    for index, char in enumerate(candidate):
        if char in "[{":
            start = index
            break
    if start < 0:
        return None
    try:
        decoded, _end = json.JSONDecoder().raw_decode(candidate[start:])
        return decoded
    except ValueError:
        return None


def parse_bridge_block(reply_text: str) -> Tuple[str, dict]:
    """剥离 ``<<<BRIDGE>>>`` 协议行。

    返回 ``(clean_text, bridge_json)``：

    - 支持行内 marker：``<<<BRIDGE>>> {"attachments":[...]}``；
    - JSON 也可写在 marker 下一行（空行/最多 8 行内）；
    - marker 后为空或坏 JSON 时返回 ``{}``，但协议行仍会从正文剥离；
    - 没有 marker 时原样返回文本 + ``{}``；
    - 正常回复（marker 之前的文本）完整保留。
    """
    raw = "" if reply_text is None else str(reply_text)
    if MARKER not in raw:
        return raw.strip(), {}

    lines = raw.splitlines()
    clean_lines: List[str] = []
    payload: Dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        marker_at = line.find(MARKER)
        if marker_at < 0:
            clean_lines.append(line)
            index += 1
            continue

        prefix = line[:marker_at].rstrip()
        if prefix.strip():
            clean_lines.append(prefix)
        rest = line[marker_at + len(MARKER):].strip()

        parsed: Optional[Any] = _decode_json_candidate(rest) if rest else None
        consumed = 0
        if parsed is None:
            # marker 行没有 JSON：向下找最多 8 行（允许空行），只在能解出 JSON 时消费
            buffer: List[str] = []
            scan = index + 1
            while scan < len(lines) and len(buffer) < 8:
                nxt = lines[scan].strip()
                if nxt and not (nxt.startswith("{") or nxt.startswith("[") or nxt.startswith('"')):
                    break
                buffer.append(lines[scan])
                scan += 1
                joined = "\n".join(buffer).strip()
                if not joined:
                    continue
                candidate = _decode_json_candidate(joined)
                if candidate is not None:
                    parsed = candidate
                    consumed = len(buffer)
                    break

        if isinstance(parsed, dict):
            if not payload:
                payload = parsed
            elif parsed.get("attachments") and not payload.get("attachments"):
                payload.update(parsed)
        elif isinstance(parsed, list):
            if not payload:
                payload = {"attachments": parsed}
        elif rest[:1] in ("[", "{"):
            _audit(
                {
                    "action": "parse_bridge",
                    "result": "bad_json",
                    "detail": rest[:500],
                }
            )

        index += 1 + consumed

    clean_text = "\n".join(clean_lines).strip("\n").rstrip()
    return clean_text, payload


# ---------------------------------------------------------------------------
# 文件校验 / 分类
# ---------------------------------------------------------------------------


def _pdf_ok(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with open(str(path), "rb") as handle:
            head = handle.read(1024)
        return head.lstrip()[:5] == b"%PDF-"
    except OSError:
        return False


def _image_ok(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with open(str(path), "rb") as handle:
            head = handle.read(16)
        if not any(head.startswith(magic) for magic in _IMAGE_MAGIC):
            return False
        try:
            from PIL import Image  # 本机有 Pillow 时做完整解码校验
        except Exception:  # noqa: BLE001 - 没装 Pillow 时退化为魔数校验
            return True
        try:
            with Image.open(str(path)) as image:
                image.verify()
            return True
        except Exception:  # noqa: BLE001
            return False
    except OSError:
        return False


def _sniff_kind(path: Path, explicit_kind: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """返回 (kind, error)。kind ∈ pdf/image/screenshot/text/file。"""
    suffix = path.suffix.lower()
    kind_hint = str(explicit_kind or "").strip().lower()
    if kind_hint == "pdf" or suffix in PDF_SUFFIXES:
        return ("pdf", None) if _pdf_ok(path) else (None, "bad_pdf")
    if kind_hint in ("image", "screenshot") or suffix in IMAGE_SUFFIXES:
        if not _image_ok(path):
            return None, "bad_image"
        return (kind_hint if kind_hint in ("image", "screenshot") else "image"), None
    if kind_hint == "text" or suffix in TEXT_SUFFIXES:
        return "text", None
    if path.is_file():
        return "file", None
    return None, "missing"


def _is_eir_name(name: str, context: Optional[dict] = None) -> bool:
    """EIR/授权码 判定。默认只看明确关键词；``context["eir_hyphen_hint"]=True``
    时把 ``订舱号-授权码.pdf`` 这种 ``-XXXX`` 命名也算 EIR。"""
    text = str(name or "").lower()
    if any(hint in text for hint in _EIR_HINTS):
        return True
    context = context or {}
    verdict = "{} {}".format(context.get("verdict") or "", context.get("判定") or "").lower()
    category = str(context.get("artifact_category") or context.get("category") or "").lower()
    platform = str(context.get("platform") or "").lower()
    if "eir" in verdict or category == "eir" or platform in ("eir", "esp-eir"):
        return True
    if context.get("eir_hyphen_hint"):
        match = re.fullmatch(r"[A-Za-z0-9]{6,20}-([A-Za-z0-9]{4,10})", Path(text).stem.upper())
        if match and re.search(r"[A-Z]", match.group(1)):
            return True
    return False


def classify_feedback(name: str, context: Optional[dict] = None) -> Optional[str]:
    """把反馈截图/证据文件名归到 约柜/无纸化/无资料/异常/其他；不确定返回 None。"""
    text = str(name or "").lower()
    context = context or {}
    platform = str(context.get("platform") or "").lower()
    verdict = "{} {}".format(context.get("verdict") or "", context.get("判定") or "").lower()

    if "约柜" in text or platform in ("约柜", "guikui", "yg"):
        return "约柜"
    if "无纸化" in text:
        if platform in ("ylt", "联运通", "ewinlu") or "联运通" in text:
            return "无纸化-联运通"
        return "无纸化-鹏海运"
    for category, keywords in _FEEDBACK_RULES:
        if category == "约柜":
            continue
        if any(keyword in text for keyword in keywords):
            return category
    if verdict:
        if "约柜" in verdict:
            return "约柜"
        for category, keywords in _FEEDBACK_RULES:
            if category == "约柜":
                continue
            if any(keyword in verdict for keyword in keywords):
                return category
    return None


def _normalize_category(
    raw_category: Optional[str], path: Path, kind: str, context: Optional[dict]
) -> str:
    raw = str(raw_category or "").strip()
    if raw in _KNOWN_CATEGORIES:
        return raw
    if kind == "pdf":
        return "eir" if _is_eir_name(path.name, context) else "print"
    if kind in ("image", "screenshot"):
        category = classify_feedback(path.name, context)
        if category:
            return category
        return str(context.get("default_category") or "其他") if context else "其他"
    if kind == "text":
        return "long_text"
    return "wechat_file"


# ---------------------------------------------------------------------------
# select_attachments
# ---------------------------------------------------------------------------


def _path_from_item(item: Any, root: Path) -> Tuple[Optional[Path], dict]:
    extra: dict = {}
    if isinstance(item, str):
        raw: Any = item
    elif isinstance(item, dict):
        extra = item
        raw = item.get("path") or item.get("file") or item.get("src") or item.get("absolute_path")
        if isinstance(raw, dict):
            raw = raw.get("path")
    else:
        return None, extra
    if not raw:
        return None, extra
    text = os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"')))
    if text.lower().startswith("file:///"):
        text = text[8:]
        if text[:1].isdigit():
            text = text.replace("|", ":", 1)
    path = Path(text)
    if not path.is_absolute():
        # DSH 常写“WeChatBridgeData\xxx”或“Desktop\WeChatBridgeData\xxx”这类相对路径，先去掉根目录前缀
        parts = list(path.parts)
        if len(parts) >= 2 and parts[0].lower() == "desktop" and parts[1] == root.name:
            parts = parts[2:]
        elif parts and parts[0] == root.name:
            parts = parts[1:]
        path = root.joinpath(*parts) if parts else root
    return path, extra


def _materialize(
    src: Path,
    kind: str,
    category: str,
    caption: Optional[str],
    root: Path,
) -> Optional[dict]:
    """把选中的源文件用 copy 归位到类别目录，返回接口约定的 dict。源文件保留。"""
    try:
        folder = _ensure_category_dir(category, root)
    except OSError as exc:
        _audit({"action": "select", "result": "mkdir_failed", "src": str(src), "error": str(exc)}, base=root)
        return None

    try:
        same_size: List[Path] = []
        if _is_within(src, folder):
            out_path = src.resolve()
        else:
            size = src.stat().st_size
            digest: Optional[str] = None
            for exist in folder.iterdir():
                try:
                    if exist.is_file() and exist.stat().st_size == size:
                        same_size.append(exist)
                except OSError:
                    continue
            if same_size:
                digest = _sha256(src)
                for exist in same_size:
                    try:
                        if _sha256(exist) == digest:
                            _audit(
                                {
                                    "action": "select",
                                    "result": "dedupe",
                                    "src": str(src),
                                    "dst": str(exist),
                                    "category": category,
                                },
                                base=root,
                            )
                            return {
                                "path": str(exist.resolve()),
                                "kind": kind,
                                "category": category,
                                "caption": str(caption or src.name),
                            }
                    except OSError:
                        continue
            out_path = _unique_path(folder / src.name)
            _copy_retry(src, out_path)
    except OSError as exc:
        _audit(
            {"action": "select", "result": "copy_failed", "src": str(src), "error": str(exc)},
            base=root,
        )
        return None

    _audit(
        {
            "action": "select",
            "result": "ok",
            "src": str(src),
            "dst": str(out_path),
            "kind": kind,
            "category": category,
        },
        base=root,
    )
    return {
        "path": str(out_path),
        "kind": kind,
        "category": category,
        "caption": str(caption or src.name),
    }


def _scan_roots(root: Path) -> List[Path]:
    roots: List[Path] = [root / DIR_PRINT, root / DIR_EIR, root / DIR_FEEDBACK]
    roots.append(root / DIR_FEEDBACK / "其他")
    for category in FEEDBACK_CATEGORIES:
        if category in ("无纸化-鹏海运", "无纸化-联运通"):
            continue
        roots.append(root / DIR_FEEDBACK / category)
    roots.append(root / DIR_FEEDBACK / "无纸化" / "鹏海运")
    roots.append(root / DIR_FEEDBACK / "无纸化" / "联运通")
    roots.append(root / LEGACY_EVIDENCE)
    roots.append(root)
    unique: List[Path] = []
    seen = set()
    for folder in roots:
        try:
            key = os.path.normcase(os.path.abspath(str(folder)))
        except OSError:
            continue
        if key in seen or not folder.is_dir():
            continue
        seen.add(key)
        unique.append(folder)
    return unique


def _normalize_token(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def _context_order(context: dict, reply_text: str) -> str:
    for key in ("so", "order_no", "order", "订舱号", "so_no"):
        token = _normalize_token(context.get(key))
        if token:
            return token
    if reply_text:
        # 从回复正文兜底提取单号：要求至少 1 个数字，避免把 <<<BRIDGE>>> 等单词当单号
        for token in re.findall(r"(?<![0-9A-Za-z])([A-Za-z0-9][A-Za-z0-9-]{5,24})(?![0-9A-Za-z])", str(reply_text)):
            if re.search(r"\d", token):
                return _normalize_token(token)
    return ""


def _platform_hints(context: dict) -> List[str]:
    hints: List[str] = []
    platform = str(context.get("platform") or "").strip().lower()
    if platform:
        hints.append(platform)
        if "esp" in platform:
            hints.append("eport")
        if "联运" in platform or "ylt" in platform:
            hints.append("联运通")
        if "约柜" in platform:
            hints.append("约柜")
    hints.extend(["联运通", "esp", "eport", "约柜", "打单", "缴费", "支付"])
    return [hint for hint in hints if hint]


def _discover_attachments(root: Path, context: dict, reply_text: str = "") -> List[dict]:
    """无 marker 时按 单号/平台/时间窗 在本轮新文件里保守找候选，最多 2 个。"""
    since = context.get("turn_start_ts")
    if since is None:
        since = context.get("since_ts")
    if since is None:
        since = context.get("started_ts")
    if since is None:
        since = time.time() - DISCOVER_WINDOW_SEC
    since = _safe_float(since, time.time() - DISCOVER_WINDOW_SEC)

    order = _context_order(context, reply_text)
    hints = _platform_hints(context)
    verdict = "{} {}".format(context.get("verdict") or "", context.get("判定") or "").lower()

    candidates: List[dict] = []
    for folder in _scan_roots(root):
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                if not path.is_file():
                    continue
                name = path.name
                if name.startswith(("_", ".")) or name == INDEX_FILE:
                    continue
                suffix = path.suffix.lower()
                # 自动发现只考虑 PDF/图片；文本（含迁移日志、_index.jsonl）必须由 marker 显式指定
                if suffix not in PDF_SUFFIXES and suffix not in IMAGE_SUFFIXES:
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since - CLOCK_SLACK_SEC:
                continue
            normalized_name = _normalize_token(name)
            if order and order not in normalized_name:
                # 有明确单号时，先只收单号命中的候选
                continue
            kind, error = _sniff_kind(path)
            if error:
                _audit(
                    {"action": "discover", "result": "rejected", "src": str(path), "error": error},
                    base=root,
                )
                continue
            assert kind is not None
            score = 0.0
            if order:
                score += 50.0
            matched_platform = any(hint and hint in name.lower() for hint in hints)
            if matched_platform:
                score += 8.0
            category = classify_feedback(name, context)
            if category and verdict and category in verdict:
                score += 6.0
            if kind == "pdf":
                tier = 0
                score += 30.0
            elif kind in ("image", "screenshot"):
                tier = 1
                score += 10.0
            elif kind == "text":
                tier = 2
            else:
                tier = 3
            score += max(0.0, min((mtime - since) / max(DISCOVER_WINDOW_SEC, 1.0), 1.0)) * 5.0
            candidates.append(
                {
                    "path": path,
                    "kind": kind,
                    "tier": tier,
                    "score": score,
                    "mtime": mtime,
                }
            )

    if order and not candidates:
        _audit(
            {
                "action": "discover",
                "result": "no_order_match",
                "order": order,
                "since_ts": since,
            },
            base=root,
        )
        return []

    candidates.sort(key=lambda item: (item["tier"], -item["score"], -item["mtime"]))
    selected: List[dict] = []
    seen_hashes = set()
    for candidate in candidates:
        if len(selected) >= ATTACH_MAX:
            break
        path = candidate["path"]
        try:
            digest = _sha256(path)
        except OSError:
            continue
        if digest in seen_hashes:  # 同一张图的不同副本只发一次
            continue
        category = _normalize_category(None, path, candidate["kind"], context)
        result = _materialize(path, candidate["kind"], category, None, root)
        if result:
            seen_hashes.add(digest)
            selected.append(result)
    _audit(
        {
            "action": "discover",
            "result": "picked",
            "count": len(selected),
            "order": order,
            "paths": [item["path"] for item in selected],
            "since_ts": since,
        },
        base=root,
    )
    return selected


def select_attachments(
    reply_text: str,
    bridge_json: Optional[dict] = None,
    context: Optional[dict] = None,
) -> List[dict]:
    """选出本轮要发回微信的附件（最多 2 个）。

    规则（DEV-INTERFACES §3）：

    1. ``bridge_json["attachments"]`` 有明确路径优先；元素可为 str 或
       ``{"path","kind","category","caption"}``。
    2. 路径不存在 / PDF 头不是 ``%PDF-`` / 图片不可解码 -> 丢弃并写审计。
    3. 选中的文件 copy 归位到对应目录，返回新路径；**源文件保留**，避免发送时被移动。
    4. 没有 marker 时按 context（单号/平台/本轮开始时间/判定）在本轮时间窗内找候选，
       最多 2 个；PDF（EIR/打单）优先于反馈截图；有明确单号时只收单号命中的文件。
    """
    context = dict(context or {})
    root = _resolve_base(context)
    try:
        ensure_layout_at(root)
    except OSError as exc:
        LOGGER.warning("ensure_layout 失败: %s", exc)

    raw_items: Any = []
    if isinstance(bridge_json, dict):
        raw_items = bridge_json.get("attachments") or []
    if not raw_items and reply_text:
        _clean, parsed = parse_bridge_block(reply_text)
        if isinstance(parsed, dict):
            raw_items = parsed.get("attachments") or []
    if isinstance(raw_items, (str, dict)):
        raw_items = [raw_items]
    if not isinstance(raw_items, (list, tuple)):
        raw_items = []

    selected: List[dict] = []
    seen_paths = set()
    for item in list(raw_items)[:10]:
        path, extra = _path_from_item(item, root)
        if path is None:
            _audit({"action": "select", "result": "rejected", "reason": "no_path", "item": item}, base=root)
            continue
        try:
            path = path.resolve()
        except OSError:
            pass
        key = os.path.normcase(str(path))
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if not path.is_file():
            _audit({"action": "select", "result": "rejected", "reason": "missing", "src": str(path)}, base=root)
            continue
        kind, error = _sniff_kind(path, extra.get("kind") if isinstance(extra, dict) else None)
        if error or kind is None:
            _audit(
                {"action": "select", "result": "rejected", "reason": error, "src": str(path)},
                base=root,
            )
            continue
        if len(selected) >= ATTACH_MAX:
            # 已选够 2 个：后面只做校验/记录，不再复制
            _audit(
                {"action": "select", "result": "skipped_max", "src": str(path)},
                base=root,
            )
            continue
        category = _normalize_category(
            extra.get("category") if isinstance(extra, dict) else None, path, kind, context
        )
        caption = extra.get("caption") if isinstance(extra, dict) else None
        result = _materialize(path, kind, category, caption, root)
        if result and result["path"] not in {item["path"] for item in selected}:
            selected.append(result)

    if not selected:
        selected = _discover_attachments(root, context, reply_text)
    return selected


# ---------------------------------------------------------------------------
# organize_recent
# ---------------------------------------------------------------------------


def _move_into(src: Path, category: str, root: Path) -> Optional[Path]:
    folder = _ensure_category_dir(category, root)
    if _is_within(src, folder):
        return src.resolve()
    try:
        dst = _unique_path(folder / _safe_filename(src.name))
        _move_retry(src, dst)
        return dst.resolve()
    except OSError as exc:
        _audit(
            {
                "action": "organize",
                "result": "move_failed",
                "src": str(src),
                "category": category,
                "error": str(exc),
            },
            base=root,
        )
        return None


def organize_recent(since_ts: float, context: Optional[dict] = None) -> List[str]:
    """把 BASE 根目录 / ``证据\\`` 里本轮新产物按规则归位；不删除、不确定就留在原处。

    - 根目录 PDF -> 打单文件；明确 EIR/授权码/设备交接 -> EIR单；
    - ``证据\\约柜_*`` -> 打单反馈截图\\约柜；名称含 无纸化/无资料/异常 -> 对应目录；
    - 其余证据按名称归到 无纸化/无资料/异常/其他，分类不出的留在原处并记审计；
    - 根目录其它文件不碰（交给对应模块/人工）。
    """
    context = dict(context or {})
    root = _resolve_base(context)
    try:
        ensure_layout_at(root)
    except OSError as exc:
        LOGGER.warning("ensure_layout 失败: %s", exc)

    try:
        since = float(since_ts)
    except (TypeError, ValueError):
        since = time.time() - DISCOVER_WINDOW_SEC

    moved: List[str] = []
    legacy = root / LEGACY_EVIDENCE
    for folder, is_legacy in ((legacy, True), (root, False)):
        if not folder.is_dir():
            continue
        try:
            entries = sorted(folder.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for path in entries:
            try:
                if not path.is_file():
                    continue
                if path.name.startswith(("_", ".")):
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since - CLOCK_SLACK_SEC:
                continue

            category: Optional[str] = None
            reason = ""
            if is_legacy:
                category = classify_feedback(path.name, context)
                reason = "legacy_evidence"
                if category is None:
                    _audit(
                        {
                            "action": "organize",
                            "result": "left_in_place",
                            "src": str(path),
                            "reason": "unclassified",
                        },
                        base=root,
                    )
                    continue
            elif path.suffix.lower() in PDF_SUFFIXES:
                if _is_eir_name(path.name, context):
                    category = "eir"
                    reason = "eir_name"
                else:
                    category = "print"
                    reason = "root_pdf"
            else:
                continue

            dst = _move_into(path, category, root)
            if dst is not None:
                moved.append(str(dst))
                _audit(
                    {
                        "action": "organize",
                        "result": "moved",
                        "src": str(path),
                        "dst": str(dst),
                        "category": category,
                        "reason": reason,
                        "since_ts": since,
                    },
                    base=root,
                )
    return moved


__all__ = [
    "BASE",
    "LONG_TEXT_THRESHOLD",
    "ATTACH_MAX",
    "FEEDBACK_CATEGORIES",
    "parse_bridge_block",
    "ensure_layout",
    "ensure_layout_at",
    "save_inbound_image",
    "save_inbound_file",
    "save_long_text",
    "select_attachments",
    "organize_recent",
    "classify_feedback",
]
