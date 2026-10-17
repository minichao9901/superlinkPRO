#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造「屏页 → 图片/图案刷屏」用的**风景照片**素材（24bpp BMP）。

为什么需要这些文件
------------------
屏页（`#panel`）的「图片/图案刷屏」除了自带的程序化图案（纯色/棋盘/色条…）之外，
还需要**真实照片**：程序化图案能验"几何/开窗/切片"对不对，但验不了照片特有的东西 ——
肤色在 RGB565 上会不会发绿（COLMOD/字节序错一眼就能看出）、渐变天空有没有色带、
细节多的树叶/浪花在色深不足时糊成什么样。所以这里放 4 类各 2 张、共 8 张照片。

尺寸按真屏各出一套（页面里的「铺满 / 适应」会自行缩放，但按原生尺寸生成最不容易糊）：

  · AXS15352 —— 240×296（4 线 SPI + DC，竖屏）
  · ST77916  —— 360×360（QSPI 圆屏；四角会被圆边切掉，所以构图尽量居中）

素材来源与许可
--------------
全部取自 **Wikimedia Commons**（CC0 / 公有领域 / CC BY / CC BY-SA），
逐张的作者与许可见 `samples/test_images/scenery/README.md`（那张表与下面 `SOURCES` 是同一份事实，
**改这里要连带改 README**）。这里只做两件事：按目标比例**居中/带焦点裁剪** + LANCZOS 缩放，
不调色、不加锐化 —— 素材要能当"颜色基准"用，动过手脚就没意义了。

用法
----
    python tools/dev/make-scenery-samples.py                # 写到 samples/test_images/scenery/
    python tools/dev/make-scenery-samples.py --out some/dir # 换目录
    python tools/dev/make-scenery-samples.py --only sky_cumulus_clouds
    python tools/dev/make-scenery-samples.py --list         # 只列来源表，不下载不生成
    make samples-scenery                                    # 同第一条

首次运行会从 Commons 下载原图（1600px 缩略图）缓存到 `--cache`（默认 `%TEMP%/scenery-src`）；
之后离线即可重跑。需要走代理时按标准环境变量来（脚本不写死代理）：

    $env:HTTPS_PROXY = 'http://127.0.0.1:7890'      # PowerShell
    make samples-scenery

依赖：Pillow（`python -m pip install pillow`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

# ---------------------------------------------------------------------------
# 真屏几何（与 app/spi/image.js 的 PANEL_GEOMETRY 保持一致；那边改了这里也要改）
# ---------------------------------------------------------------------------
PANELS = {
    'axs15352': (240, 296),
    'st77916': (360, 360),
}

# ---------------------------------------------------------------------------
# 来源表（唯一事实源）：focus = 裁剪窗口在**原图里**的相对位置，0=靠左/靠上、1=靠右/靠下、
# 0.5=居中。竖屏/方屏都要裁，所以两个方向都可能用得上（人像主要靠 focus 保住脸）。
# ---------------------------------------------------------------------------
SOURCES = [
    dict(key='flowers_wildflower_meadow', group='花朵绿树', focus=(0.50, 0.55),
         title='During peak bloom a colorful combination of wildflowers bloom together, '
               'including Sitka valerian, lupine, and magenta paintbrush. '
               '(57071e16-bead-4548-b247-34de9d62c8c2).JPG',
         author='NPS Photo', license='Public domain', license_url='',
         page='https://commons.wikimedia.org/wiki/File:During_peak_bloom_a_colorful_combination_of_wildflowers_bloom_together%2C_including_Sitka_valerian%2C_lupine%2C_and_magenta_paintbrush._%2857071e16-bead-4548-b247-34de9d62c8c2%29.JPG',
         note='高山野花甸：色彩多、有细碎花瓣，看 RGB565 下小花瓣会不会糊成一片'),
    dict(key='greenery_forest_path', group='花朵绿树', focus=(0.50, 0.50),
         title='Forest path surrounded by green trees.jpg',
         author='Zed Can77', license='CC0',
         license_url='https://creativecommons.org/publicdomain/zero/1.0/deed.en',
         page='https://commons.wikimedia.org/wiki/File:Forest_path_surrounded_by_green_trees.jpg',
         note='林间小路：大面积绿色 + 逆光树叶，看绿色阶调与细节'),
    dict(key='beauty_portrait_outdoor', group='美女', focus=(0.18, 0.45),
         title='Outdoor portrait (Unsplash).jpg',
         author='Anthony Ginsbrook', license='CC0',
         license_url='https://creativecommons.org/publicdomain/zero/1.0/deed.en',
         page='https://commons.wikimedia.org/wiki/File:Outdoor_portrait_%28Unsplash%29.jpg',
         note='街拍人像：肤色是 RGB565 最挑的地方（发绿=色序/字节序错）'),
    dict(key='beauty_portrait_umbrella', group='美女', focus=(0.50, 0.04),
         title='Russia, Moscow Oblast. Young woman with umbrella, studio portrait.jpg',
         author='Dmitry Makeev', license='CC BY-SA 4.0',
         license_url='https://creativecommons.org/licenses/by-sa/4.0/',
         page='https://commons.wikimedia.org/wiki/File:Russia%2C_Moscow_Oblast._Young_woman_with_umbrella%2C_studio_portrait.jpg',
         note='棚拍人像：大面积纯蓝背景，色偏一眼可见'),
    dict(key='sky_cumulus_clouds', group='蓝天白云', focus=(0.50, 0.50),
         title='White Cumulus Clouds against Blue Sky (2).jpg',
         author='Lance Vanlewen', license='CC BY-SA 4.0',
         license_url='https://creativecommons.org/licenses/by-sa/4.0/',
         page='https://commons.wikimedia.org/wiki/File:White_Cumulus_Clouds_against_Blue_Sky_%282%29.jpg',
         note='蓝天积云：深蓝渐变最容易出色带（RGB565 只有 5/6/5 位）'),
    dict(key='sky_sun_and_cloud', group='蓝天白云', focus=(0.50, 0.50),
         title='White Cumulus Clouds against Blue Sky with Sun Shining.jpg',
         author='Lance Vanlewen', license='CC BY-SA 4.0',
         license_url='https://creativecommons.org/licenses/by-sa/4.0/',
         page='https://commons.wikimedia.org/wiki/File:White_Cumulus_Clouds_against_Blue_Sky_with_Sun_Shining.jpg',
         note='太阳 + 云：高光过曝区与蓝天的边界，看白色不偏色'),
    dict(key='sea_turquoise_bay', group='大海', focus=(0.50, 0.55),
         title='Turquoise sea at San Gemiliano beach, Tortolì, Sardinia, Italy.jpg',
         author='Fabio Pani', license='CC BY-SA 4.0',
         license_url='https://creativecommons.org/licenses/by-sa/4.0/',
         page='https://commons.wikimedia.org/wiki/File:Turquoise_sea_at_San_Gemiliano_beach%2C_Tortol%C3%AC%2C_Sardinia%2C_Italy.jpg',
         note='青绿海面：海水的青→蓝过渡，看青绿色偏不偏'),
    dict(key='sea_tropical_beach', group='大海', focus=(0.50, 0.55),
         title='Malapascua (island), Tropical beach, Turquoise water, Philippines.jpg',
         author='Vyacheslav Argenberg', license='CC BY 4.0',
         license_url='https://creativecommons.org/licenses/by/4.0/',
         page='https://commons.wikimedia.org/wiki/File:Malapascua_%28island%29%2C_Tropical_beach%2C_Turquoise_water%2C_Philippines.jpg',
         note='热带海滩：白沙滩 + 椰林 + 蓝天，三种亮部同时压 RGB565'),
]

UA = 'web-serial-rtt-tools-sample-fetch/1.0 (offline panel sample images)'
FILE_PATH = 'https://commons.wikimedia.org/wiki/Special:FilePath/{title}?width={w}'


# ---------------------------------------------------------------------------
# 下载（带缓存；只读网络，失败重试有上限）
# ---------------------------------------------------------------------------
def fetch(title: str, cache: Path, width: int = 1600, tries: int = 4) -> Path:
    slug = hashlib.sha1(title.encode('utf-8')).hexdigest()[:12]
    dst = cache / f'{slug}.jpg'
    if dst.exists() and dst.stat().st_size > 10_000:
        return dst
    cache.mkdir(parents=True, exist_ok=True)
    url = FILE_PATH.format(title=urllib.parse.quote(title), w=width)
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=60) as r:      # 走环境变量里的代理
                data = r.read()
            if len(data) < 10_000:
                raise RuntimeError(f'下载内容过小：{len(data)} B')
            tmp = dst.with_suffix('.part')
            tmp.write_bytes(data)
            tmp.replace(dst)
            time.sleep(0.5)                                        # 对 Commons 客气一点
            return dst
        except Exception as e:                                      # noqa: BLE001
            last = e
            print(f'    重试 {k + 1}/{tries}：{e}')
            time.sleep(2 * (k + 1))
    raise RuntimeError(f'下载失败：{title} —— {last}')


# ---------------------------------------------------------------------------
# 裁剪 + 缩放（cover：先按目标比例裁到最大可用窗口，再 LANCZOS 缩放）
# ---------------------------------------------------------------------------
def cover(im: Image.Image, w: int, h: int, focus: tuple[float, float]) -> Image.Image:
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    cw, ch = min(sw, round(w / scale)), min(sh, round(h / scale))
    fx, fy = focus
    x0 = round((sw - cw) * fx)
    y0 = round((sh - ch) * fy)
    return im.crop((x0, y0, x0 + cw, y0 + ch)).resize((w, h), Image.LANCZOS)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def cat(key: str) -> str:
    """控制台只打 ASCII 分类词（flowers / greenery / beauty / sky / sea）。

    中文分类名（花朵绿树 / 美女 / …）保留在 `manifest.json` 与 README 里 ——
    控制台在 GBK/UTF-8 之间换来换去时中文必花，日志读不了，分类词又不需要翻译。
    """
    return key.split('_', 1)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description='造屏页用的风景照片素材（24bpp BMP）')
    ap.add_argument('--out', default='samples/test_images/scenery', help='输出根目录')
    ap.add_argument('--cache', default=os.path.join(os.environ.get('TEMP', '/tmp'), 'scenery-src'),
                    help='原图缓存目录')
    ap.add_argument('--only', action='append', default=None, help='只处理某个 key（可重复）')
    ap.add_argument('--list', action='store_true', help='只列来源表')
    ap.add_argument('--manifest', action='store_true', help='额外写 manifest.json（含 sha256）')
    args = ap.parse_args()

    keys = set(args.only) if args.only else None
    srcs = [s for s in SOURCES if not keys or s['key'] in keys]
    if keys:
        unknown = keys - {s['key'] for s in SOURCES}
        if unknown:
            print('未知 key：', ', '.join(sorted(unknown)))
            return 2

    if args.list:
        for s in srcs:
            print(f"{cat(s['key']):<9} {s['key']:<30} {s['license']:<18} {s['title'][:60]}")
        return 0

    out_root = Path(args.out)
    cache = Path(args.cache)
    manifest = []
    for s in srcs:
        print(f"[{cat(s['key'])}] {s['key']}  ({s['license']})")
        src = fetch(s['title'], cache)
        im = Image.open(src).convert('RGB')
        print(f"    原图 {im.width}×{im.height}  focus={s['focus']}")
        for panel, (w, h) in PANELS.items():
            out_dir = out_root / panel
            out_dir.mkdir(parents=True, exist_ok=True)
            dst = out_dir / f"{s['key']}.bmp"
            cover(im, w, h, s['focus']).save(dst, format='BMP')      # 24bpp（Pillow 默认）
            mb = dst.stat().st_size / 1024
            print(f"    -> {dst}  {w}×{h}  {mb:.0f} KB")
            manifest.append(dict(panel=panel, file=str(dst).replace('\\', '/'),
                                 w=w, h=h, bytes=dst.stat().st_size, sha256=sha256(dst),
                                 **{k: s[k] for k in ('key', 'group', 'title', 'author',
                                                      'license', 'license_url', 'page', 'note')}))
    if args.manifest:
        mp = out_root / 'manifest.json'
        mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        print('->', mp)
    print(f'完成：{len(manifest)} 个文件')
    return 0


if __name__ == '__main__':
    sys.exit(main())
