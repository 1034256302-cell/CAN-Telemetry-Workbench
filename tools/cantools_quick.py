from __future__ import annotations

import csv
import datetime
import fnmatch
import io
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import cantools
import cantools.logreader
from cantools.database.can.database import Database as CanDatabase
from cantools.database.can.message import Message

try:
    import can  # type: ignore
    HAS_PYTHON_CAN = True
except ImportError:
    HAS_PYTHON_CAN = False


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "quick_outputs"
DEFAULT_DBC = ROOT / "tests" / "files" / "dbc" / "motohawk.dbc"
SAMPLE_LOG = ROOT / "sample.log"

# 七大中文分类子文件夹
OUTPUT_DIRS: dict[str, Path] = {
    "dbc_info": OUTPUT_DIR / "1_DBC数据库文档",
    "decoded_txt": OUTPUT_DIR / "2_日志解码文本_TXT",
    "decoded_csv": OUTPUT_DIR / "3_日志解码表格_CSV",
    "plots": OUTPUT_DIR / "4_信号波形图_PNG",
    "c_source": OUTPUT_DIR / "5_生成C语言代码",
    "encoded_frames": OUTPUT_DIR / "6_信号反向编码帧",
    "log_reports": OUTPUT_DIR / "7_日志体检与周期报告",
}

# 旧英文目录到新中文目录的映射（自动迁移使用）
LEGACY_DIR_MAP: dict[str, str] = {
    "dbc_info": "dbc_info",
    "decoded_txt": "decoded_txt",
    "decoded_csv": "decoded_csv",
    "plots": "plots",
    "c_source": "c_source",
    "encoded_frames": "encoded_frames",
    "log_reports": "log_reports",
}


def ensure_outputs() -> None:
    """创建七大中文分类输出文件夹，并自动迁移旧文件与旧英文目录。"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    for sub_dir in OUTPUT_DIRS.values():
        sub_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / ".mplconfig").mkdir(exist_ok=True)
    migrate_legacy_outputs()


def migrate_legacy_outputs() -> list[tuple[Path, Path]]:
    """将散落文件及旧英文子目录中的文件自动归档至七大中文分类目录。"""
    moved: list[tuple[Path, Path]] = []
    if not OUTPUT_DIR.exists():
        return moved

    # 1. 迁移旧英文子目录中的文件
    for old_name, key in LEGACY_DIR_MAP.items():
        old_dir = OUTPUT_DIR / old_name
        target_dir = OUTPUT_DIRS[key]
        if old_dir.exists() and old_dir.is_dir() and old_dir != target_dir:
            for item in list(old_dir.iterdir()):
                if item.is_file():
                    dest = target_dir / item.name
                    if dest.exists():
                        dest.unlink()
                    shutil.move(str(item), str(dest))
                    moved.append((item, dest))
            try:
                old_dir.rmdir()
            except OSError:
                pass

    # 2. 迁移 quick_outputs 根目录下的散落文件
    for item in list(OUTPUT_DIR.iterdir()):
        if not item.is_file():
            continue

        name_lower = item.name.lower()
        target_dir: Path | None = None

        if name_lower.endswith(("_dump.txt", "_list.txt")):
            target_dir = OUTPUT_DIRS["dbc_info"]
        elif name_lower.endswith("_decoded.txt"):
            target_dir = OUTPUT_DIRS["decoded_txt"]
        elif name_lower.endswith("_decoded.csv"):
            target_dir = OUTPUT_DIRS["decoded_csv"]
        elif name_lower.endswith(("_report.csv", "_report.txt")):
            target_dir = OUTPUT_DIRS["log_reports"]
        elif name_lower.endswith("_encoded.txt"):
            target_dir = OUTPUT_DIRS["encoded_frames"]
        elif name_lower.endswith((".png", ".jpg", ".svg", ".pdf")):
            target_dir = OUTPUT_DIRS["plots"]
        elif name_lower.endswith((".c", ".h", "_c_source.txt", "_build_log.txt")):
            target_dir = OUTPUT_DIRS["c_source"]

        if target_dir is not None:
            dest = target_dir / item.name
            if dest.exists():
                dest.unlink()
            shutil.move(str(item), str(dest))
            moved.append((item, dest))

    return moved


def load_database_smart(
    dbc_path: Path,
    encoding: str | None = None,
    strict: bool = False,
) -> tuple[CanDatabase, str]:
    """智能加载 CAN 数据库（自动识别 UTF-8 / GBK / CP1252 编码，默认开启非严格容错）。"""
    encodings_to_try = (
        [encoding]
        if encoding
        else ["utf-8", "utf-8-sig", "gb18030", "gbk", "cp1252", "latin-1"]
    )
    last_exc: Exception | None = None

    for enc in encodings_to_try:
        try:
            with dbc_path.open("r", encoding=enc) as check_fp:
                content = check_fp.read()
            db = cantools.database.load_string(
                content,
                database_format=dbc_path.suffix.lstrip(".").lower() or None,
                strict=strict,
            )
            if isinstance(db, CanDatabase):
                return db, enc
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
        except Exception as exc:
            last_exc = exc
            if strict:
                try:
                    db = cantools.database.load_file(
                        dbc_path, encoding=enc, strict=False
                    )
                    if isinstance(db, CanDatabase):
                        return db, enc
                except Exception as inner_exc:
                    last_exc = inner_exc

    db = cantools.database.load_file(dbc_path, encoding=encoding, strict=False)
    if not isinstance(db, CanDatabase):
        raise ValueError(f"Unsupported database type: {type(db).__name__} ({last_exc})")
    return db, encoding or "cp1252"


def _detect_text_log_encoding(log_path: Path) -> str:
    """自动检测文本日志文件的编码（支持 UTF-16 LE/BE BOM、UTF-8 BOM、UTF-8 及 GB18030）。"""
    try:
        raw_head = log_path.read_bytes()[:4096]
    except Exception:
        return "utf-8"

    if raw_head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if raw_head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw_head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def iter_log_frames(
    log_path: Path,
    keep_unknowns: bool = True,
) -> Iterator[tuple[str, cantools.logreader.DataFrame | None]]:
    """逐帧读取 .log (candump)、.trc (PCAN)、.asc (Vector) 或 .blf (二进制) 日志文件。"""
    suffix = log_path.suffix.lower()

    if suffix == ".blf":
        if not HAS_PYTHON_CAN:
            raise RuntimeError("读取 .blf 二进制日志需要安装 python-can。")
        with can.BLFReader(str(log_path)) as reader:
            for msg in reader:
                ts = datetime.timedelta(seconds=float(msg.timestamp))
                channel = str(msg.channel) if msg.channel is not None else "can0"
                frame = cantools.logreader.DataFrame(
                    channel=channel,
                    frame_id=int(msg.arbitration_id),
                    is_extended_frame=bool(msg.is_extended_id),
                    data=bytes(msg.data),
                    is_remote_frame=bool(msg.is_remote_frame),
                    timestamp=ts,
                    timestamp_format=cantools.logreader.TimestampFormat.RELATIVE,
                )
                hex_data = " ".join(f"{b:02X}" for b in frame.data)
                raw_line = f"({msg.timestamp:012.6f})  {channel}  {frame.frame_id:X}   [{len(frame.data)}]  {hex_data}"
                yield raw_line, frame
        return

    enc = _detect_text_log_encoding(log_path)
    with log_path.open("r", encoding=enc, errors="replace") as handle:
        parser = cantools.logreader.Parser(handle)
        for raw_line, frame in parser.iterlines(keep_unknowns=keep_unknowns):
            yield raw_line, frame


def timestamp_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime.timedelta):
        return f"{value.total_seconds():.6f}"
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return str(value)


def timestamp_to_seconds(
    value: object,
    first_dt: datetime.datetime | None = None,
) -> tuple[float | None, datetime.datetime | None]:
    if value is None:
        return None, first_dt
    if isinstance(value, datetime.timedelta):
        return value.total_seconds(), first_dt
    if isinstance(value, datetime.datetime):
        if first_dt is None:
            first_dt = value
        return (value - first_dt).total_seconds(), first_dt
    return None, first_dt


def lookup_message(db: CanDatabase, frame: cantools.logreader.DataFrame) -> Message:
    """在数据库中查找报文（自动兼容标准帧与扩展帧 ID）。"""
    try:
        return db.get_message_by_frame_id(
            frame.frame_id, force_extended_id=frame.is_extended_frame
        )
    except KeyError:
        return db.get_message_by_frame_id(frame.frame_id)


def write_sample_log(target_path: Path = SAMPLE_LOG) -> Path:
    target_path.write_text(
        "\n".join(
            [
                "(000.000000)  vcan0  1F0   [8]  81 06 00 00 00 00 00 00",
                "(001.000000)  vcan0  1F0   [8]  95 83 00 00 00 00 00 00",
                "(002.000000)  vcan0  1F0   [8]  A8 00 00 00 00 00 00 00",
                "(003.000000)  vcan0  1F0   [8]  BC 7D 00 00 00 00 00 00",
                "(004.000000)  vcan0  1F0   [8]  D0 FA 00 00 00 00 00 00",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return target_path


@dataclass
class CsvExportResult:
    output_path: Path
    total_lines: int
    decoded_frames: int
    unknown_id_frames: int
    invalid_lines: int
    unknown_ids: set[int] = field(default_factory=set)


def export_log_to_csv(
    dbc_path: Path,
    log_path: Path,
    output_name: str | None = None,
    filter_frame_id: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> CsvExportResult:
    """流式将 CAN 日志解码导出为 CSV 表格（保存在 3_日志解码表格_CSV）。"""
    ensure_outputs()
    if not output_name:
        output_name = f"{log_path.stem}_decoded.csv"
    if not output_name.lower().endswith(".csv"):
        output_name += ".csv"
    output_path = OUTPUT_DIRS["decoded_csv"] / output_name

    db, _ = load_database_smart(dbc_path, strict=False)

    all_signal_names: set[str] = set()
    for msg in db.messages:
        if filter_frame_id is not None and msg.frame_id != filter_frame_id:
            continue
        for sig in msg.signals:
            all_signal_names.add(sig.name)

    base_columns = [
        "timestamp",
        "timestamp_sec",
        "channel",
        "frame_id",
        "message",
        "raw_data",
        "status",
        "raw_line",
    ]
    columns = base_columns + sorted(all_signal_names)

    total_lines = 0
    decoded_frames = 0
    unknown_id_frames = 0
    invalid_lines = 0
    unknown_ids: set[int] = set()
    first_dt: datetime.datetime | None = None

    with output_path.open("w", newline="", encoding="utf-8-sig") as out_fp:
        writer = csv.DictWriter(out_fp, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()

        for raw_line, frame in iter_log_frames(log_path, keep_unknowns=True):
            if not raw_line.strip() and frame is None:
                continue

            total_lines += 1
            row: dict[str, object] = {
                "timestamp": "",
                "timestamp_sec": "",
                "channel": "",
                "frame_id": "",
                "message": "",
                "raw_data": "",
                "status": "",
                "raw_line": raw_line,
            }

            if frame is None:
                if raw_line.lstrip().startswith((";", "date ", "base ", "internal ", "Begin ", "End ", "//")):
                    total_lines -= 1
                    continue
                invalid_lines += 1
                row["status"] = "invalid_log_line"
                writer.writerow(row)
                continue

            if filter_frame_id is not None and frame.frame_id != filter_frame_id:
                continue

            ts_sec, first_dt = timestamp_to_seconds(frame.timestamp, first_dt)
            row["timestamp"] = timestamp_to_text(frame.timestamp)
            row["timestamp_sec"] = f"{ts_sec:.6f}" if ts_sec is not None else ""
            row["channel"] = frame.channel
            row["frame_id"] = f"0x{frame.frame_id:X}"
            row["raw_data"] = " ".join(f"{byte:02X}" for byte in frame.data)

            try:
                message = lookup_message(db, frame)
            except KeyError:
                unknown_id_frames += 1
                unknown_ids.add(frame.frame_id)
                row["status"] = f"unknown_frame_id: 0x{frame.frame_id:X}"
                writer.writerow(row)
                continue

            try:
                decoded = message.decode(
                    frame.data,
                    allow_truncated=True,
                    allow_excess=True,
                )
            except Exception as exc:
                row["message"] = message.name
                row["status"] = f"decode_error: {exc}"
                writer.writerow(row)
                continue

            row["message"] = message.name
            row["status"] = "ok"
            decoded_frames += 1

            if isinstance(decoded, dict):
                for sig_name, sig_val in decoded.items():
                    row[sig_name] = sig_val

            writer.writerow(row)

            if progress_callback and total_lines % 500 == 0:
                progress_callback(total_lines, decoded_frames)

    if progress_callback:
        progress_callback(total_lines, decoded_frames)

    return CsvExportResult(
        output_path=output_path,
        total_lines=total_lines,
        decoded_frames=decoded_frames,
        unknown_id_frames=unknown_id_frames,
        invalid_lines=invalid_lines,
        unknown_ids=unknown_ids,
    )


def export_log_to_text(
    dbc_path: Path,
    log_path: Path,
    output_name: str | None = None,
    single_line: bool = False,
) -> Path:
    """将 CAN 日志解码为可读文本（保存在 2_日志解码文本_TXT）。"""
    ensure_outputs()
    if not output_name:
        output_name = f"{log_path.stem}_decoded.txt"
    output_path = OUTPUT_DIRS["decoded_txt"] / output_name

    db, _ = load_database_smart(dbc_path, strict=False)

    with output_path.open("w", encoding="utf-8") as out_fp:
        for raw_line, frame in iter_log_frames(log_path, keep_unknowns=True):
            if frame is None:
                if raw_line.strip():
                    out_fp.write(raw_line + "\n")
                continue

            try:
                msg = lookup_message(db, frame)
                decoded = msg.decode(frame.data, allow_truncated=True, allow_excess=True)
                if isinstance(decoded, dict):
                    sig_parts = []
                    for sig in msg.signals:
                        if sig.name in decoded:
                            val = decoded[sig.name]
                            unit = f" {sig.unit}" if sig.unit and not isinstance(val, str) else ""
                            sig_parts.append(f"{sig.name}: {val}{unit}")
                    if single_line:
                        out_fp.write(f"{raw_line} :: {msg.name}({', '.join(sig_parts)})\n")
                    else:
                        joined = ",\n    ".join(sig_parts)
                        out_fp.write(f"{raw_line} ::\n{msg.name}(\n    {joined}\n)\n")
                else:
                    out_fp.write(f"{raw_line} :: {msg.name}({decoded})\n")
            except KeyError:
                out_fp.write(f"{raw_line} :: Unknown frame id {frame.frame_id} (0x{frame.frame_id:x})\n")
            except Exception as exc:
                out_fp.write(f"{raw_line} :: Decode error: {exc}\n")

    return output_path


def export_dbc_info(dbc_path: Path, mode: str = "dump") -> Path:
    """导出 DBC 详细结构或列表（保存在 1_DBC数据库文档）。"""
    ensure_outputs()
    suffix = "list" if mode.lower().startswith("l") else "dump"
    output_path = OUTPUT_DIRS["dbc_info"] / f"{dbc_path.stem}_{suffix}.txt"
    _, detected_enc = load_database_smart(dbc_path, strict=False)
    subcmd = "list" if suffix == "list" else "dump"
    args = [subcmd, "--no-strict", "-e", detected_enc, str(dbc_path)]
    if subcmd == "list":
        args.insert(1, "-a")
    rc = cantools_command(*args, output_path=output_path)
    if rc != 0 and subcmd == "dump":
        # 若非标 DBC 在 dump 位图时超界失败，自动回退为 list -a 保证文档完整输出
        cantools_command("list", "-a", "--no-strict", "-e", detected_enc, str(dbc_path), output_path=output_path)
    return output_path


def export_c_source(dbc_path: Path) -> list[Path]:
    """生成嵌入式 C 代码 .c 和 .h（保存在 5_生成C语言代码）。"""
    ensure_outputs()
    c_dir = OUTPUT_DIRS["c_source"]
    _, detected_enc = load_database_smart(dbc_path, strict=False)
    log_file = c_dir / f"{dbc_path.stem}_build_log.txt"

    before_mtimes = {
        f: f.stat().st_mtime for f in c_dir.iterdir() if f.is_file() and f.suffix.lower() in (".c", ".h")
    }

    rc = cantools_command(
        "generate_c_source",
        "--no-strict",
        "-e",
        detected_enc,
        "-o",
        str(c_dir),
        str(dbc_path),
        output_path=log_file,
    )
    if rc != 0:
        err_msg = log_file.read_text(encoding="utf-8", errors="replace").strip() if log_file.exists() else f"exit code {rc}"
        raise RuntimeError(f"生成 C 语言代码失败：{err_msg}")

    # 优先收集本次新生成或更新的 .c / .h 文件（兼容带连字符或驼峰文件名被转为下划线的情况）
    updated = sorted(
        f
        for f in c_dir.iterdir()
        if f.is_file()
        and f.suffix.lower() in (".c", ".h")
        and (f not in before_mtimes or f.stat().st_mtime >= before_mtimes[f])
    )
    if updated:
        return updated

    generated = sorted(
        f for f in c_dir.glob(f"{dbc_path.stem}*.*") if f.suffix.lower() in (".c", ".h")
    )
    return generated if generated else sorted(f for f in c_dir.iterdir() if f.suffix.lower() in (".c", ".h"))


def _match_requested_signal(full_name: str, short_name: str, patterns: list[str]) -> bool:
    for pat in patterns:
        pat_clean = pat.split(":")[0].strip()
        if not pat_clean or pat_clean in ("-", ","):
            continue
        if "." in pat_clean:
            if fnmatch.fnmatchcase(full_name.lower(), pat_clean.lower()):
                return True
        else:
            if fnmatch.fnmatchcase(short_name.lower(), pat_clean.lower()) or fnmatch.fnmatchcase(
                full_name.lower(), f"*.{pat_clean.lower()}"
            ):
                return True
    return False


def export_signal_plot(
    dbc_path: Path,
    log_path: Path,
    signals: list[str],
    output_name: str = "signals.png",
) -> Path:
    """
    使用内置 Matplotlib 引擎直接绘制信号波形图（保存在 4_信号波形图_PNG）：
    - 彻底摆脱外部子进程与 pythonw 无控制台句柄报错；
    - 支持中文字体显示、支持枚举型信号（如 Enable）、支持单点/多点自动标记；
    - 多信号独立分栏（Subplots）展示，时间轴清晰不重叠。
    """
    ensure_outputs()
    if not output_name.lower().endswith((".png", ".pdf", ".svg")):
        output_name += ".png"
    output_path = OUTPUT_DIRS["plots"] / output_name

    os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".mplconfig"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    db, _ = load_database_smart(dbc_path, strict=False)
    patterns = [s for s in signals if s.strip()] or ["*"]

    # 建立信号元数据表
    sig_meta: dict[str, tuple[str, dict[int, str] | None]] = {}
    for msg in db.messages:
        for sig in msg.signals:
            key = f"{msg.name}.{sig.name}"
            choices_map: dict[int, str] | None = None
            if sig.choices:
                choices_map = {int(k): str(v) for k, v in sig.choices.items()}
            sig_meta[key] = (sig.unit or "", choices_map)

    series_x: dict[str, list[float]] = {}
    series_y: dict[str, list[float]] = {}

    first_dt: datetime.datetime | None = None
    frame_idx = 0
    used_line_numbers = False

    for _, frame in iter_log_frames(log_path, keep_unknowns=False):
        if frame is None:
            continue
        frame_idx += 1
        ts_sec, first_dt = timestamp_to_seconds(frame.timestamp, first_dt)
        if ts_sec is None:
            ts_val = float(frame_idx)
            used_line_numbers = True
        else:
            ts_val = ts_sec

        try:
            msg = lookup_message(db, frame)
            # decode_choices=False 保证枚举信号也能拿到数值，再结合 choices_map 标注刻度
            decoded_num = msg.decode(
                frame.data,
                decode_choices=False,
                scaling=True,
                allow_truncated=True,
                allow_excess=True,
            )
        except Exception:
            continue

        if not isinstance(decoded_num, dict):
            continue

        for sig_name, val in decoded_num.items():
            full_name = f"{msg.name}.{sig_name}"
            if not _match_requested_signal(full_name, sig_name, patterns):
                continue
            try:
                num_val = float(val)
            except (TypeError, ValueError):
                continue
            series_x.setdefault(full_name, []).append(ts_val)
            series_y.setdefault(full_name, []).append(num_val)

    if not series_x:
        raise ValueError(
            f"在日志 {log_path.name} 中未找到所选信号 ({', '.join(patterns)}) 的有效数据帧。\n"
            f"请确认日志中的 CAN ID 与当前 DBC ({dbc_path.name}) 匹配。"
        )

    plotted_keys = list(series_x.keys())
    n_plots = len(plotted_keys)
    fig_height = max(4.2, min(2.6 * n_plots + 1.2, 14.0))
    fig, axes = plt.subplots(n_plots, 1, figsize=(8.8, fig_height), sharex=True, squeeze=False)
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2"]

    for idx, full_name in enumerate(plotted_keys):
        ax = axes[idx, 0]
        xs = series_x[full_name]
        ys = series_y[full_name]
        unit, choices_map = sig_meta.get(full_name, ("", None))
        color = colors[idx % len(colors)]

        unit_label = f" [{unit}]" if unit and unit != "-" else ""
        label_text = f"{full_name}{unit_label}"
        marker = "o" if len(xs) <= 40 else None

        if choices_map:
            ax.step(xs, ys, where="post", label=label_text, color=color, linewidth=1.8, marker=marker)
            yticks = sorted(choices_map.keys())
            ax.set_yticks(yticks)
            ax.set_yticklabels([f"{k} ({choices_map[k]})" for k in yticks])
            ax.margins(y=0.25)
        else:
            ax.plot(xs, ys, label=label_text, color=color, linewidth=1.8, marker=marker, markersize=4)

        ax.set_ylabel(full_name.split(".")[-1] + unit_label, fontsize=9)
        ax.grid(True, linestyle="--", alpha=0.45)
        ax.legend(loc="upper left", fontsize=9, framealpha=0.85)

    axes[0, 0].set_title(f"CAN 信号波形图 — {log_path.name}", fontsize=11, fontweight="bold", pad=10)
    axes[-1, 0].set_xlabel("帧序号 (Frame #)" if used_line_numbers else "相对时间 (秒 / s)", fontsize=10)

    fig.tight_layout()
    fig.savefig(str(output_path), dpi=115, bbox_inches="tight")
    plt.close(fig)

    return output_path


@dataclass
class FrameIdStats:
    frame_id: int
    is_extended: bool = False
    message_name: str = ""
    dbc_defined: bool = False
    dbc_cycle_ms: float | None = None
    count: int = 0
    channels: set[str] = field(default_factory=set)
    dlcs: set[int] = field(default_factory=set)
    first_ts: float | None = None
    last_ts: float | None = None
    prev_ts: float | None = None
    intervals_ms: list[float] = field(default_factory=list)


@dataclass
class LogHealthReport:
    log_path: Path
    dbc_path: Path | None
    total_frames: int
    decoded_frames: int
    unknown_frames: int
    duration_sec: float
    frame_stats: list[FrameIdStats]
    csv_report_path: Path
    txt_report_path: Path


def analyze_log_health(
    log_path: Path,
    dbc_path: Path | None = None,
) -> LogHealthReport:
    """扫描 CAN 日志并生成体检与周期统计报告（保存在 7_日志体检与周期报告）。"""
    ensure_outputs()
    db: CanDatabase | None = None
    if dbc_path and dbc_path.exists():
        db, _ = load_database_smart(dbc_path, strict=False)

    stats_by_id: dict[int, FrameIdStats] = {}
    total_frames = 0
    decoded_frames = 0
    unknown_frames = 0
    first_dt: datetime.datetime | None = None
    global_first_ts: float | None = None
    global_last_ts: float | None = None

    for _, frame in iter_log_frames(log_path, keep_unknowns=False):
        if frame is None:
            continue

        total_frames += 1
        ts_sec, first_dt = timestamp_to_seconds(frame.timestamp, first_dt)
        if ts_sec is not None:
            if global_first_ts is None or ts_sec < global_first_ts:
                global_first_ts = ts_sec
            if global_last_ts is None or ts_sec > global_last_ts:
                global_last_ts = ts_sec

        st = stats_by_id.get(frame.frame_id)
        if st is None:
            msg_name = "<DBC未定义>"
            dbc_defined = False
            dbc_cycle: float | None = None
            if db is not None:
                try:
                    msg = lookup_message(db, frame)
                    msg_name = msg.name
                    dbc_defined = True
                    dbc_cycle = float(msg.cycle_time) if msg.cycle_time is not None else None
                except KeyError:
                    pass
            st = FrameIdStats(
                frame_id=frame.frame_id,
                is_extended=frame.is_extended_frame,
                message_name=msg_name,
                dbc_defined=dbc_defined,
                dbc_cycle_ms=dbc_cycle,
            )
            stats_by_id[frame.frame_id] = st

        st.count += 1
        if st.dbc_defined:
            decoded_frames += 1
        else:
            unknown_frames += 1

        if frame.channel:
            st.channels.add(frame.channel)
        st.dlcs.add(len(frame.data))

        if ts_sec is not None:
            if st.first_ts is None:
                st.first_ts = ts_sec
            if st.prev_ts is not None and ts_sec >= st.prev_ts:
                st.intervals_ms.append((ts_sec - st.prev_ts) * 1000.0)
            st.prev_ts = ts_sec
            st.last_ts = ts_sec

    duration_sec = (
        (global_last_ts - global_first_ts)
        if (global_first_ts is not None and global_last_ts is not None and global_last_ts >= global_first_ts)
        else 0.0
    )

    sorted_stats = sorted(stats_by_id.values(), key=lambda s: s.frame_id)

    csv_report_path = OUTPUT_DIRS["log_reports"] / f"{log_path.stem}_report.csv"
    txt_report_path = OUTPUT_DIRS["log_reports"] / f"{log_path.stem}_report.txt"

    csv_fields = [
        "frame_id_hex",
        "frame_id_dec",
        "is_extended",
        "message_name",
        "dbc_status",
        "frame_count",
        "channels",
        "dlc_bytes",
        "avg_cycle_ms",
        "min_cycle_ms",
        "max_cycle_ms",
        "dbc_cycle_ms",
        "first_time_s",
        "last_time_s",
    ]

    with csv_report_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=csv_fields)
        writer.writeheader()
        for st in sorted_stats:
            avg_ms = sum(st.intervals_ms) / len(st.intervals_ms) if st.intervals_ms else None
            min_ms = min(st.intervals_ms) if st.intervals_ms else None
            max_ms = max(st.intervals_ms) if st.intervals_ms else None
            writer.writerow(
                {
                    "frame_id_hex": f"0x{st.frame_id:X}",
                    "frame_id_dec": st.frame_id,
                    "is_extended": "Yes" if st.is_extended else "No",
                    "message_name": st.message_name,
                    "dbc_status": "已匹配" if st.dbc_defined else "未定义",
                    "frame_count": st.count,
                    "channels": ",".join(sorted(st.channels)),
                    "dlc_bytes": ",".join(str(d) for d in sorted(st.dlcs)),
                    "avg_cycle_ms": f"{avg_ms:.2f}" if avg_ms is not None else "-",
                    "min_cycle_ms": f"{min_ms:.2f}" if min_ms is not None else "-",
                    "max_cycle_ms": f"{max_ms:.2f}" if max_ms is not None else "-",
                    "dbc_cycle_ms": f"{st.dbc_cycle_ms:.0f}" if st.dbc_cycle_ms is not None else "-",
                    "first_time_s": f"{st.first_ts:.6f}" if st.first_ts is not None else "-",
                    "last_time_s": f"{st.last_ts:.6f}" if st.last_ts is not None else "-",
                }
            )

    lines = [
        "======================================================================",
        f"CAN 日志体检与周期统计报告: {log_path.name}",
        "======================================================================",
        f"日志文件: {log_path}",
        f"DBC 文件: {dbc_path if dbc_path else '未指定'}",
        f"总有效帧数: {total_frames} 帧 (已匹配 DBC: {decoded_frames} 帧, 未定义 ID: {unknown_frames} 帧)",
        f"日志总时长: {duration_sec:.6f} 秒",
        f"包含 CAN ID 种类: {len(sorted_stats)} 种",
        "----------------------------------------------------------------------",
        f"{'CAN ID':<10} {'报文名称':<22} {'状态':<8} {'帧数':<8} {'平均周期(ms)':<14} {'DBC周期(ms)':<12}",
        "----------------------------------------------------------------------",
    ]
    for st in sorted_stats:
        avg_str = f"{sum(st.intervals_ms)/len(st.intervals_ms):.2f}" if st.intervals_ms else "-"
        dbc_c_str = f"{st.dbc_cycle_ms:.0f}" if st.dbc_cycle_ms is not None else "-"
        status_str = "已匹配" if st.dbc_defined else "未定义"
        lines.append(
            f"0x{st.frame_id:<8X} {st.message_name:<22} {status_str:<8} {st.count:<8} {avg_str:<14} {dbc_c_str:<12}"
        )
    lines.append("======================================================================\n")
    txt_report_path.write_text("\n".join(lines), encoding="utf-8")

    return LogHealthReport(
        log_path=log_path,
        dbc_path=dbc_path,
        total_frames=total_frames,
        decoded_frames=decoded_frames,
        unknown_frames=unknown_frames,
        duration_sec=duration_sec,
        frame_stats=sorted_stats,
        csv_report_path=csv_report_path,
        txt_report_path=txt_report_path,
    )


def encode_signal_dict(
    dbc_path: Path,
    message_name: str,
    values: dict[str, object],
) -> tuple[str, Path]:
    """将信号物理值编码为 CAN 报文帧（保存在 6_信号反向编码帧）。"""
    ensure_outputs()
    db, _ = load_database_smart(dbc_path, strict=False)
    message = db.get_message_by_name(message_name)

    normalized_values: dict[str, object] = {}
    for sig in message.signals:
        raw_val = values.get(sig.name)
        if raw_val is None or raw_val == "":
            raw_val = sig.initial if sig.initial is not None else (sig.minimum if sig.minimum is not None else sig.offset)
        # 若信号存在枚举定义，且用户输入了浮点形式的整数（如 1.0），自动转为整数以匹配枚举表
        if sig.choices and isinstance(raw_val, float) and raw_val.is_integer():
            raw_val = int(raw_val)
        normalized_values[sig.name] = raw_val

    data = message.encode(normalized_values, strict=False)
    frame = " ".join(f"{byte:02X}" for byte in data)
    result = (
        f"Message: {message.name}\n"
        f"Frame ID: 0x{message.frame_id:X} ({message.frame_id})\n"
        f"Extended Frame: {message.is_extended_frame}\n"
        f"Length: {len(data)} bytes\n"
        f"Data (Hex): {frame}\n"
        f"candump line: (000.000000)  vcan0  {message.frame_id:X}   [{len(data)}]  {frame}\n"
    )
    output_path = OUTPUT_DIRS["encoded_frames"] / f"{message.name}_encoded.txt"
    output_path.write_text(result, encoding="utf-8")
    return result, output_path


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause() -> None:
    input("\nPress Enter to continue...")


def normalize_path(value: str) -> Path:
    value = value.strip().strip('"').strip("'")
    return Path(value).expanduser()


def ask_path(prompt: str, default: Path | None = None, must_exist: bool = True) -> Path:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip()
        path = default if not value and default else normalize_path(value)

        if path is None:
            print("Please enter a path.")
            continue

        if not path.is_absolute():
            path = ROOT / path

        if must_exist and not path.exists():
            print(f"Not found: {path}")
            print("Tip: you can drag a file into this window to paste its path.")
            continue

        return path


def ask_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default or ""


def _resolve_console_python() -> str:
    """确保子进程调用使用 python.exe 而非无控制台的 pythonw.exe。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def cantools_command(
    *args: str,
    stdin_path: Path | None = None,
    output_path: Path | None = None,
) -> int:
    """安全调用 cantools CLI 子命令，兼容 pythonw 无控制台模式。"""
    env = os.environ.copy()
    env.setdefault("MPLBACKEND", "Agg")
    env.setdefault("MPLCONFIGDIR", str(ROOT / ".mplconfig"))

    command = [_resolve_console_python(), "-m", "cantools", *args]
    if sys.stdout is not None:
        try:
            print("\nRunning:")
            print(" ".join(shlex.quote(part) for part in command))
        except Exception:
            pass

    stdin_handle = (
        stdin_path.open("r", encoding="utf-8", errors="replace")
        if stdin_path
        else None
    )
    stdout_handle = output_path.open("w", encoding="utf-8") if output_path else None
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            creationflags=creationflags,
        )
    finally:
        if stdin_handle:
            stdin_handle.close()
        if stdout_handle:
            stdout_handle.close()

    if output_path and sys.stdout is not None:
        try:
            print(f"\nSaved: {output_path}")
        except Exception:
            pass

    return result.returncode


def parse_signal_value(value: str) -> object:
    value = value.strip()

    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    try:
        return int(value, 0)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        pass

    return value.strip('"').strip("'")


def parse_signal_values(raw_values: str) -> dict[str, object]:
    values: dict[str, object] = {}

    for item in raw_values.split(","):
        item = item.strip()
        if not item:
            continue

        if "=" not in item:
            raise ValueError(f"Expected name=value, got: {item}")

        name, value = item.split("=", 1)
        values[name.strip()] = parse_signal_value(value)

    return values


def show_database() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    mode = ask_text("Show simple list or detailed dump? type list/dump", "dump").lower()
    out = export_dbc_info(dbc, mode=mode)
    if mode.startswith("l"):
        print(out.read_text(encoding="utf-8", errors="replace"))


def decode_log() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    log = ask_path("CAN log file", SAMPLE_LOG)
    out = export_log_to_text(dbc, log)
    print(f"\nSaved: {out}")


def plot_log() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    log = ask_path("CAN log file", SAMPLE_LOG)
    db, _ = load_database_smart(dbc, strict=False)
    print("\nAvailable signals in DBC:")
    default_sig = "Temperature"
    for msg in db.messages:
        sigs = ", ".join(s.name for s in msg.signals)
        print(f"  [{msg.name} (0x{msg.frame_id:X})]: {sigs}")
        if msg.signals and default_sig == "Temperature" and "Temperature" not in [s.name for s in msg.signals]:
            default_sig = msg.signals[0].name

    signals = ask_text("Signals to plot, separated by spaces", default_sig)
    output_name = ask_text("Output image name", f"{log.stem}_signals.png")
    out = export_signal_plot(dbc, log, shlex.split(signals), output_name)
    print(f"\nImage saved: {out}")


def generate_c_source() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    files = export_c_source(dbc)
    print("\nGenerated C source files:")
    for f in files:
        print(f"  - {f}")


def encode_message() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    db, _ = load_database_smart(dbc, strict=False)

    print("\nMessages in this DBC:")
    for message in db.messages:
        signal_names = ", ".join(signal.name for signal in message.signals)
        print(f"- {message.name}  ID=0x{message.frame_id:X}  signals: {signal_names}")

    default_msg = db.messages[0].name if db.messages else ""
    message_name = ask_text("Message name", default_msg)

    print("\nEnter signal values as comma-separated name=value pairs.")
    print("Example: Temperature=250.1, AverageRadius=3.2, Enable=1")
    raw_values = ask_text("Signal values")
    values = parse_signal_values(raw_values)

    result, output = encode_signal_dict(dbc, message_name, values)
    print()
    print(result)
    print(f"Saved: {output}")


def decode_log_to_csv() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    log = ask_path("CAN log file", SAMPLE_LOG)
    output_name = ask_text("Output CSV name", f"{log.stem}_decoded.csv")
    res = export_log_to_csv(dbc, log, output_name=output_name)
    print(f"\nCSV saved: {res.output_path}")
    print(
        f"Total frames: {res.total_lines} | Decoded: {res.decoded_frames} | "
        f"Unknown IDs: {res.unknown_id_frames} | Invalid lines: {res.invalid_lines}"
    )


def run_log_health_check() -> None:
    dbc = ask_path("DBC file", DEFAULT_DBC)
    log = ask_path("CAN log file", SAMPLE_LOG)
    report = analyze_log_health(log, dbc)
    print()
    print(report.txt_report_path.read_text(encoding="utf-8"))
    print(f"CSV report saved: {report.csv_report_path}")
    print(f"TXT report saved: {report.txt_report_path}")


def main() -> int:
    ensure_outputs()

    if "--gui" in sys.argv:
        from cantools_gui import launch_gui
        launch_gui()
        return 0

    while True:
        clear_screen()
        print("cantools quick menu")
        print("===================")
        print(f"Project: {ROOT}")
        print(f"Outputs: {OUTPUT_DIR} (七大中文分类子目录)")
        print()
        print("1. Create sample CAN log")
        print("2. View DBC messages/signals          -> quick_outputs/1_DBC数据库文档/")
        print("3. Decode CAN log to text             -> quick_outputs/2_日志解码文本_TXT/")
        print("4. Plot signals to PNG                -> quick_outputs/4_信号波形图_PNG/")
        print("5. Generate C source (.c/.h)          -> quick_outputs/5_生成C语言代码/")
        print("6. Encode signal values to CAN frame  -> quick_outputs/6_信号反向编码帧/")
        print("7. Decode CAN log to CSV              -> quick_outputs/3_日志解码表格_CSV/")
        print("8. CAN log health & cycle time report -> quick_outputs/7_日志体检与周期报告/")
        print("9. Launch Desktop Visual GUI (启动图形界面)")
        print("0. Exit")
        print()

        choice = input("Choose an option: ").strip()
        print()

        try:
            if choice == "1":
                path = write_sample_log()
                print(f"Sample log saved: {path}")
            elif choice == "2":
                show_database()
            elif choice == "3":
                decode_log()
            elif choice == "4":
                plot_log()
            elif choice == "5":
                generate_c_source()
            elif choice == "6":
                encode_message()
            elif choice == "7":
                decode_log_to_csv()
            elif choice == "8":
                run_log_health_check()
            elif choice == "9":
                from cantools_gui import launch_gui
                launch_gui()
            elif choice == "0":
                return 0
            else:
                print("Unknown option.")
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as exc:
            print(f"\nError: {exc}")

        pause()


if __name__ == "__main__":
    raise SystemExit(main())
