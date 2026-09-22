"""系统字体枚举：Web UI 的字体选择器要列出「运行 Carme 的这台机器」的真实字体。

为什么放在后端：网页可能跑在手机或另一台电脑上，只有后端知道本机装了什么字体。
这里不依赖 fontconfig 等外部工具，直接解析字体文件的 SFNT 表
（name / OS/2 / post），macOS 与 Linux 通用；解析不到时退回 fc-list，
再退回内置清单，保证界面上永远有可选项。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 字体文件后缀：.ttc/.otc 是字体集合，一个文件里有多个字面。
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
# 字体很少变化，进程内缓存一小时，避免每次打开设置都扫描磁盘。
CACHE_TTL_SECONDS = 3600.0
MAX_FONTS = 2000
MAX_FILES = 20000  # 一次扫描的字体文件上限，防止异常目录把接口拖死

MACOS_DIRS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
)
# 按需下载的系统字体（Font Services 资源包）都在这两个前缀下。
MACOS_GLOBS = (
    "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData",
    "/System/Library/PrivateFrameworks/FontServices.framework/Resources/Fonts",
)
LINUX_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
)
USER_DIRS = (
    "~/Library/Fonts",
    "~/.local/share/fonts",
    "~/.fonts",
)

# 系统占位字体与符号字体，不该出现在用户的选择列表里。
SKIP_FAMILIES = {"lastresort", ".lastresort", "last resort",
                 "apple braille", "apple color emoji"}

# 所选字体在本机缺失（例如手机打开网页）时的兜底字体。
SANS_FALLBACK = ('-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", '
                 '"Microsoft YaHei", "Noto Sans CJK SC", sans-serif')
MONO_FALLBACK = '"SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace'

# name 表里我们关心的字段：16/17 是排版用的家族名，1/2 是兼容用的旧字段。
NAME_FAMILY, NAME_SUBFAMILY, NAME_FULL, NAME_TYPO_FAMILY = 1, 2, 4, 16
KNOWN_NAME_IDS = {NAME_FAMILY, NAME_SUBFAMILY, NAME_FULL, NAME_TYPO_FAMILY}
# 平台语言 ID：界面用中文名，CSS 用英文名；简繁分开存，展示时优先简体。
WINDOWS_LANGUAGES = {0x0409: "en", 0x0809: "en", 0x0804: "zh-CN",
                     0x0404: "zh-TW", 0x0C04: "zh-HK", 0x1404: "zh-HK"}

# fc-list / 扫描都失败时的保底清单：名称必须是各平台通用的英文名。
BUILTIN_FONTS = (
    ("PingFang SC", "苹方", False),
    ("Hiragino Sans GB", "冬青黑体", False),
    ("Songti SC", "宋体", False),
    ("Kaiti SC", "楷体", False),
    ("Helvetica Neue", "Helvetica Neue", False),
    ("Arial", "Arial", False),
    ("Noto Sans CJK SC", "Noto Sans CJK", False),
    ("DejaVu Sans", "DejaVu Sans", False),
    ("Menlo", "Menlo", True),
    ("SF Mono", "SF Mono", True),
    ("Consolas", "Consolas", True),
    ("DejaVu Sans Mono", "DejaVu Sans Mono", True),
)

_cache: tuple[float, dict] | None = None


# --------------------------------------------------------------------------- #
#  对外接口
# --------------------------------------------------------------------------- #


def font_stack(family: str, monospace: bool = False) -> str:
    """把字体名转成 CSS font-family 值，带兜底字体。"""
    quoted = '"' + family.replace('"', "").strip() + '"'
    return f"{quoted}, {MONO_FALLBACK if monospace else SANS_FALLBACK}"


def system_fonts(dirs: list[str] | None = None, *, use_cache: bool = True) -> dict:
    """返回运行 Carme 的机器的字体列表。

    dirs 显式传入时（或设置 CARME_FONT_DIRS）只扫描这些目录，供测试与自定义环境使用。
    """
    global _cache
    now = time.time()
    if use_cache and dirs is None and _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    roots = _scan_dirs(dirs)
    fonts = _scan_files(roots)
    source = "files"
    if not fonts:
        fonts = _from_fontconfig()
        source = "fontconfig" if fonts else "builtin"
    if not fonts:
        fonts = [{"family": family, "label": label, "monospace": mono, "faces": 1,
                  "stack": font_stack(family, mono)} for family, label, mono in BUILTIN_FONTS]

    fonts.sort(key=lambda item: (item["monospace"], item["label"].casefold(), item["family"]))
    result = {
        "fonts": fonts[:MAX_FONTS],
        "count": min(len(fonts), MAX_FONTS),
        "source": source,
        "platform": platform.system().lower(),
        # macOS 的按需字体分散在上百个哈希目录里，只回报前几个给排查用。
        "dirs": [str(root) for root in roots[:8]],
        "dir_count": len(roots),
    }
    if dirs is None:
        _cache = (now, result)
    return result


def clear_cache() -> None:
    """测试与「重新检测」按钮用。"""
    global _cache
    _cache = None


# --------------------------------------------------------------------------- #
#  目录与文件
# --------------------------------------------------------------------------- #


def _scan_dirs(dirs: list[str] | None) -> list[Path]:
    if dirs is not None:
        candidates = [Path(item).expanduser() for item in dirs]
    else:
        override = os.getenv("CARME_FONT_DIRS", "").strip()
        if override:
            candidates = [Path(item).expanduser() for item in override.split(os.pathsep) if item.strip()]
        elif sys.platform.startswith("win"):
            candidates = [Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts"]
        else:
            system_dirs = MACOS_DIRS if sys.platform == "darwin" else LINUX_DIRS
            candidates = [Path(item) for item in system_dirs] + _glob_dirs()
            candidates += [Path(item).expanduser() for item in USER_DIRS]
    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        if path.is_dir() and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _glob_dirs() -> list[Path]:
    """macOS 把苹方等按需字体放在 AssetsV2 的资源目录里，路径带哈希，只能按模式找。"""
    found: list[Path] = []
    for pattern in MACOS_GLOBS:
        try:
            found += [path for path in Path("/").glob(pattern.lstrip("/")) if path.is_dir()]
        except OSError:
            continue
    return sorted(found)


def _scan_files(roots: list[Path]) -> list[dict]:
    """扫描目录并解析每个字体文件；按家族名去重，重复字面只累加计数。"""
    collected: dict[str, dict] = {}
    seen = 0
    for root in roots:
        for path in _walk_fonts(root):
            if seen >= MAX_FILES:
                return list(collected.values())
            seen += 1
            for face in _faces(path):
                family = face["family"]
                entry = collected.get(family)
                if entry is None:
                    collected[family] = {**face, "faces": 1}
                else:
                    entry["faces"] += 1
    return list(collected.values())


def _walk_fonts(root: Path):
    """只走真实目录：软链目录在 3.11/3.12 的 Path.rglob 下会被跟随，可能无限递归。"""
    def keep_going(_error: OSError) -> None:
        return None

    for directory, subdirs, files in os.walk(root, followlinks=False, onerror=keep_going):
        subdirs.sort()
        for name in sorted(files):
            if Path(name).suffix.lower() not in FONT_SUFFIXES:
                continue
            path = Path(directory) / name
            try:
                # is_file 会排除目录、设备、FIFO 与断链；不会为打开文件而阻塞。
                if path.is_file():
                    yield path
            except OSError:
                continue


# --------------------------------------------------------------------------- #
#  字体文件解析（SFNT：TrueType / OpenType / 字体集合）
# --------------------------------------------------------------------------- #


def _faces(path: Path) -> list[dict]:
    """读出一个文件里的所有字面；解析失败就当这个文件不存在。"""
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
            if len(header) < 12:
                return []
            offsets = [0]
            if header[:4] == b"ttcf":
                count = int.from_bytes(header[8:12], "big")
                if not 1 <= count <= 256:
                    return []
                offsets = [int.from_bytes(header[12:16], "big")]
                tail = handle.read(4 * (count - 1))
                offsets += [int.from_bytes(tail[index * 4:index * 4 + 4], "big")
                            for index in range(min(count - 1, len(tail) // 4))]
            faces = []
            for offset in offsets:
                face = _face(handle, offset)
                if face is not None:
                    faces.append(face)
            return faces
    except (OSError, ValueError):
        return []


def _face(handle, offset: int) -> dict | None:
    handle.seek(offset)
    header = handle.read(12)
    if len(header) < 12:
        return None
    num_tables = int.from_bytes(header[4:6], "big")
    if not 1 <= num_tables <= 1024:
        return None
    handle.seek(offset + 12)
    directory = handle.read(16 * num_tables)
    if len(directory) < 16 * num_tables:
        return None
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(num_tables):
        record = directory[index * 16:(index + 1) * 16]
        tables[record[:4]] = (int.from_bytes(record[8:12], "big"),
                              int.from_bytes(record[12:16], "big"))
    names = _name_records(handle, tables.get(b"name"))
    family, label = _pick_family(names)
    if not family or family.strip().casefold() in SKIP_FAMILIES or family.startswith("."):
        return None
    monospace = _is_monospace(handle, tables)
    return {"family": family, "label": label or family, "monospace": monospace,
            "stack": font_stack(family, monospace)}


def _name_records(handle, table: tuple[int, int] | None) -> dict[int, dict[str, str]]:
    """解析 name 表：返回 {nameID: {语言: 文本}}。"""
    if not table:
        return {}
    offset, length = table
    if length <= 0 or length > 8_000_000:
        return {}
    handle.seek(offset)
    raw = handle.read(length)
    if len(raw) < 6:
        return {}
    count = int.from_bytes(raw[2:4], "big")
    storage = int.from_bytes(raw[4:6], "big")
    records: dict[int, dict[str, str]] = {}
    for index in range(min(count, 4096)):
        base = 6 + index * 12
        if base + 12 > len(raw):
            break
        platform_id = int.from_bytes(raw[base:base + 2], "big")
        encoding_id = int.from_bytes(raw[base + 2:base + 4], "big")
        language_id = int.from_bytes(raw[base + 4:base + 6], "big")
        name_id = int.from_bytes(raw[base + 6:base + 8], "big")
        size = int.from_bytes(raw[base + 8:base + 10], "big")
        start = storage + int.from_bytes(raw[base + 10:base + 12], "big")
        if name_id not in KNOWN_NAME_IDS or size <= 0 or start + size > len(raw):
            continue
        language = _language(platform_id, language_id)
        if language is None:
            continue
        text = _decode(raw[start:start + size], platform_id, encoding_id)
        if text:
            records.setdefault(name_id, {}).setdefault(language, text)
    return records


def _decode(chunk: bytes, platform_id: int, encoding_id: int) -> str:
    # platform 0/3 的记录统一按 UTF-16BE 解：编码 0/1/2/3/6 是 BMP，4/10 的超出部分也这么存。
    if platform_id in (0, 3) and encoding_id in {0, 1, 2, 3, 4, 6, 10}:
        text = chunk.decode("utf-16-be", errors="ignore")
    else:
        text = chunk.decode("mac-roman", errors="ignore")
    text = "".join(char for char in text if char >= " " and char != "\x7f").strip()
    return text[:80]


def _language(platform_id: int, language_id: int) -> str | None:
    if platform_id == 3 or platform_id == 0:
        return WINDOWS_LANGUAGES.get(language_id, "en" if language_id == 0x0000 else None)
    if platform_id == 1:
        # Mac 平台只信任英文（语言 ID 0）；其它语言的 MacRoman 文本可能已经乱码。
        return "en" if language_id == 0 else None
    return None


def _pick_family(names: dict[int, dict[str, str]]) -> tuple[str, str]:
    """返回 (CSS 用的英文名, 界面显示的名字)。"""
    for name_id in (NAME_TYPO_FAMILY, NAME_FAMILY):
        record = names.get(name_id) or {}
        latin = (record.get("en") or "").strip()
        local = _local_name(record)
        if latin or local:
            return (latin or local), (local or latin)
    return "", ""


def _local_name(record: dict[str, str]) -> str:
    """中文名优先简体：同一个字体常有简繁两套名字，界面选大陆习惯的那个。"""
    for language in ("zh-CN", "zh-TW", "zh-HK"):
        text = (record.get(language) or "").strip()
        if text:
            return text


def _is_monospace(handle, tables: dict[bytes, tuple[int, int]]) -> bool:
    """post.isFixedPitch 最可靠；没有就退到 OS/2 的 Panose 比例位。"""
    post = tables.get(b"post")
    if post and post[1] >= 16:  # isFixedPitch 在 post 表第 12 字节，表更短就不能读
        handle.seek(post[0] + 12)
        value = handle.read(4)
        if len(value) == 4 and int.from_bytes(value, "big") != 0:
            return True
    os2 = tables.get(b"OS/2")
    if os2 and os2[1] >= 36:  # Panose 从第 32 字节开始，读 4 字节需要 36
        handle.seek(os2[0] + 32)
        panose = handle.read(4)
        if len(panose) == 4 and panose[3] == 9:  # bProportion = Monospaced
            return True
    return False


# --------------------------------------------------------------------------- #
#  fc-list 回退（Linux 常见；macOS 装了 fontconfig 才有）
# --------------------------------------------------------------------------- #


def _from_fontconfig() -> list[dict]:
    binary = shutil.which("fc-list")
    if not binary:
        return []
    try:
        done = subprocess.run([binary, "--format", "%{family[0]}|%{spacing}\n"],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    collected: dict[str, dict] = {}
    for line in done.stdout.splitlines():
        family, _, spacing = line.partition("|")
        family = family.strip().lstrip("@")
        if not family or family.casefold() in SKIP_FAMILIES or family.startswith("."):
            continue
        if family not in collected:
            monospace = spacing.strip() == "100"
            collected[family] = {"family": family, "label": family, "monospace": monospace,
                                 "faces": 0, "stack": font_stack(family, monospace)}
        collected[family]["faces"] += 1
    return list(collected.values())
