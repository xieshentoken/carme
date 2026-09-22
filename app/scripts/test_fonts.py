"""隔离验收系统字体枚举与 /api/fonts：用合成字体文件跑，不依赖本机装了什么字体。"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"

import httpx  # noqa: E402
import yaml  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402

from carme import config as config_module  # noqa: E402
from carme import fonts as fonts_module  # noqa: E402
from carme.api.routes import build_router, require_token  # noqa: E402
from carme.bus import EventBus  # noqa: E402
from carme.runtime import Runtime  # noqa: E402
from carme.store import Store  # noqa: E402

WINDOWS, UNICODE_BMP = 3, 1


def name_table(records: list[tuple[int, int, int, str]]) -> bytes:
    """records: (语言 ID, nameID, mac/win 平台, 文本)。"""
    payload = b""
    entries = b""
    for language, name_id, platform, text in records:
        encoded = text.encode("utf-16-be")
        entries += (platform.to_bytes(2, "big") + UNICODE_BMP.to_bytes(2, "big")
                    + language.to_bytes(2, "big") + name_id.to_bytes(2, "big")
                    + len(encoded).to_bytes(2, "big") + len(payload).to_bytes(2, "big"))
        payload += encoded
    header = (0).to_bytes(2, "big") + len(records).to_bytes(2, "big") + (6 + 12 * len(records)).to_bytes(2, "big")
    return header + entries + payload


def post_table(monospace: bool) -> bytes:
    return (0x00020000).to_bytes(4, "big") + (0).to_bytes(4, "big") + (0).to_bytes(4, "big") \
        + (1 if monospace else 0).to_bytes(4, "big")


def os2_table(monospace: bool) -> bytes:
    # PostScript 的 Panose 字段：bProportion 在第 4 个字节，9 表示等宽。
    return (0).to_bytes(32, "big") + bytes([2, 0, 5, 9 if monospace else 3, 0, 0, 0, 0, 0, 0])


def sfnt(tables: dict[bytes, bytes]) -> bytes:
    directory = b""
    body = b""
    offset = 12 + 16 * len(tables)
    for tag, payload in tables.items():
        directory += tag + (0).to_bytes(4, "big") + offset.to_bytes(4, "big") + len(payload).to_bytes(4, "big")
        padded = payload + b"\0" * ((-len(payload)) % 4)
        body += padded
        offset += len(padded)
    return b"\x00\x01\x00\x00" + len(tables).to_bytes(2, "big") + (0).to_bytes(6, "big") + directory + body


def collection(faces: list[bytes]) -> bytes:
    """字体集合（.ttc）：一个文件里多个字面，表偏移是相对整个文件而不是相对字面。"""
    start = 12 + 4 * len(faces)
    offsets: list[int] = []
    cursor = start
    for face in faces:
        offsets.append(cursor)
        cursor += len(face) + ((-len(face)) % 4)
    head = b"ttcf" + (0x00010000).to_bytes(4, "big") + len(faces).to_bytes(4, "big")
    return head + b"".join(item.to_bytes(4, "big") for item in offsets) \
        + b"".join(rebase(face, offset) for face, offset in zip(faces, offsets))


def rebase(face: bytes, delta: int) -> bytes:
    count = int.from_bytes(face[4:6], "big")
    out = bytearray(face)
    for index in range(count):
        base = 12 + 16 * index
        offset = int.from_bytes(face[base + 8:base + 12], "big")
        out[base + 8:base + 12] = (offset + delta).to_bytes(4, "big")
    return bytes(out)


def write_fixtures(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fixture-sans.ttf").write_bytes(sfnt({
        b"name": name_table([(0x0409, 1, WINDOWS, "Fixture Sans"), (0x0409, 16, WINDOWS, "Fixture Sans"),
                             (0x0804, 16, WINDOWS, "夹具无衬线")]),
        b"post": post_table(False),
    }))
    (root / "fixture-mono.ttf").write_bytes(sfnt({
        b"name": name_table([(0x0409, 1, WINDOWS, "Fixture Mono")]),
        b"post": post_table(True),
    }))
    # 没有 post、只有 OS/2 的 Panose 比例位：仍要认出等宽；同时验证繁体名不会被当默认。
    (root / "fixture-panose.ttf").write_bytes(sfnt({
        b"name": name_table([(0x0409, 1, WINDOWS, "Fixture Code"), (0x0404, 1, WINDOWS, "夾具碼")]),
        b"OS/2": os2_table(True),
    }))
    # 隐藏字体与占位字体不进入列表。
    (root / "system-hidden.ttf").write_bytes(sfnt({
        b"name": name_table([(0x0409, 1, WINDOWS, ".Hidden Fixture")]),
        b"post": post_table(False),
    }))
    (root / "lastresort.otf").write_bytes(sfnt({
        b"name": name_table([(0x0409, 1, WINDOWS, "LastResort")]),
        b"post": post_table(False),
    }))
    # 字体集合：同一家族的三个字面（Regular / Bold / Light）只算一个家族。
    faces = [sfnt({b"name": name_table([(0x0409, 1, WINDOWS, "Fixture Collection")]),
                   b"post": post_table(False)}) for _ in range(3)]
    (root / "fixture-collection.ttc").write_bytes(collection(faces))
    # 坏文件：截断的 sfnt 不能让整次扫描失败。
    (root / "broken.ttf").write_bytes(b"\x00\x01\x00\x00\x00\x02")
    (root / "not-a-font.txt").write_text("ignore me")
    # platform 0（Unicode）记录同样按 UTF-16BE 解，解错中文名会变成乱码
    (root / "fixture-unicode.ttf").write_bytes(sfnt({
        b"name": name_table([(0x0409, 16, 0, "Fixture Unicode"), (0x0804, 16, 0, "统一码夹具")]),
        b"post": post_table(False),
    }))
    # 软链目录指回自己：扫描不能跟着跑飞（Python 3.11/3.12 的 rglob 会跟随软链）
    (root / "loop").symlink_to(root, target_is_directory=True)


def local_scan(root: Path) -> dict[str, dict]:
    result = fonts_module.system_fonts([str(root)])
    return {item["family"]: item for item in result["fonts"]}


def unit_checks(root: Path) -> None:
    write_fixtures(root)
    fonts = local_scan(root)
    assert set(fonts) == {"Fixture Sans", "Fixture Mono", "Fixture Code", "Fixture Collection",
                          "Fixture Unicode"}, sorted(fonts)
    assert fonts["Fixture Unicode"]["label"] == "统一码夹具", fonts["Fixture Unicode"]
    assert fonts["Fixture Unicode"]["family"] == "Fixture Unicode", fonts["Fixture Unicode"]
    assert fonts["Fixture Sans"]["label"] == "夹具无衬线", fonts["Fixture Sans"]
    assert fonts["Fixture Sans"]["monospace"] is False
    assert fonts["Fixture Mono"]["monospace"] is True, fonts["Fixture Mono"]
    assert fonts["Fixture Code"]["monospace"] is True, fonts["Fixture Code"]
    assert fonts["Fixture Code"]["family"] == "Fixture Code", "CSS 用英文名"
    assert fonts["Fixture Code"]["label"] == "夾具碼", "没有简体名时退回繁体名"
    assert fonts["Fixture Collection"]["faces"] == 3, fonts["Fixture Collection"]
    families = [item["family"] for item in fonts_module.system_fonts([str(root)])["fonts"]]
    assert families.index("Fixture Mono") > families.index("Fixture Sans"), "等宽字体排在比例字体之后"
    assert '"Fixture Sans",' in fonts["Fixture Sans"]["stack"] and "sans-serif" in fonts["Fixture Sans"]["stack"]
    assert fonts["Fixture Mono"]["stack"].endswith("monospace")
    print("PASS: 解析 TTF / 字体集合 / Panose 等宽 / 隐藏与坏文件都不影响结果")

    empty = fonts_module.system_fonts([str(root / "missing")])
    assert empty["fonts"], "扫不到字体时必须回退，不能让界面没有可选项"
    assert empty["source"] in {"fontconfig", "builtin"}, empty["source"]
    assert '"PingFang SC",' in fonts_module.font_stack("PingFang SC")
    assert fonts_module.font_stack('Quote"d', monospace=True).startswith('"Quoted",')
    print("PASS: 目录为空时回退 fontconfig 或内置清单，字体名会做 CSS 转义")


async def api_checks(root: Path, font_dir: Path) -> None:
    config_module.CONFIG_DIR = root / "config"
    config_module.ENV_FILE = root / ".env"
    config_module.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (config_module.CONFIG_DIR / "models.yaml").write_text(
        yaml.safe_dump({"providers": {}, "models": [], "tiers": {}, "budget": {}}), encoding="utf-8")
    (config_module.CONFIG_DIR / "agents.yaml").write_text(yaml.safe_dump(
        {"agents": {"chief": {"name": "Chief", "prompt": "fixture", "tier": "balanced",
                              "sandbox": "none", "entry": True, "tools": []}}}), encoding="utf-8")
    config = config_module.load(reload=True)
    store = Store(root / "data" / "carme.db")
    runtime = Runtime(config, store, EventBus())
    os.environ["CARME_TOKEN"] = "font-fixture-token"
    # 后端只认 CARME_FONT_DIRS，这样验收不会受本机字体影响。
    os.environ["CARME_FONT_DIRS"] = str(font_dir)
    fonts_module.clear_cache()
    app = FastAPI()
    app.include_router(build_router(config, store, runtime), dependencies=[Depends(require_token)])
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://carme.test",
                               headers={"Authorization": "Bearer font-fixture-token"})
    try:
        listed = await client.get("/api/fonts")
        assert listed.status_code == 200, listed.text
        payload = listed.json()
        assert payload["count"] == len(payload["fonts"]) == 5, payload
        assert payload["source"] == "files" and payload["platform"], payload
        assert all(item["family"] and item["stack"] for item in payload["fonts"])
        assert str(font_dir) in payload["dirs"]
        anonymous = await httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                           base_url="http://carme.test").get("/api/fonts")
        assert anonymous.status_code == 401, anonymous.text
        (font_dir / "fixture-late.ttf").write_bytes(sfnt({
            b"name": name_table([(0x0409, 1, WINDOWS, "Fixture Late")]), b"post": post_table(False)}))
        assert (await client.get("/api/fonts")).json()["count"] == 5, "缓存期内不该重复扫描"
        reloaded = await client.post("/api/fonts/reload")
        assert reloaded.status_code == 200 and reloaded.json()["count"] == 6, reloaded.text
        print("PASS: GET /api/fonts 需要令牌、按目录返回结果，POST /api/fonts/reload 会重新扫描")
    finally:
        await client.aclose()
        await runtime.shutdown()
        store.close()
        os.environ.pop("CARME_FONT_DIRS", None)
        os.environ.pop("CARME_TOKEN", None)
        fonts_module.clear_cache()


def real_machine_check() -> None:
    """本机真的存在字体目录时，默认扫描路径也必须有结果。"""
    dirs = fonts_module._scan_dirs(None)
    if not dirs:
        print("SKIP: 这台机器上没有已知字体目录")
        return
    result = fonts_module.system_fonts(use_cache=False)
    assert result["count"] > 0 and result["source"] in {"files", "fontconfig"}, result
    assert all(item["faces"] >= 1 for item in result["fonts"]), "每个家族至少要有一个字面"
    print(f"PASS: 本机扫描到 {result['count']} 个字体家族（{result['source']}），"
          f"含 {sum(1 for item in result['fonts'] if item['monospace'])} 个等宽")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="carme-fonts-") as directory:
        root = Path(directory)
        unit_checks(root / "fixtures")
        asyncio.run(api_checks(root, root / "fixtures"))
    real_machine_check()
