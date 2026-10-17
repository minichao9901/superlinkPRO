#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造「屏页 → 动画 / 视频」用的示例素材（GIF / APNG / 动画 WebP / MP4 / WebM）。

为什么需要这些文件
------------------
屏页（`#panel`）的「动画 / 视频」是**逐帧整屏刷**：源可以是浏览器 `<video>` 能吃下的
容器（MP4 / WebM），也可以走 `ImageDecoder`（GIF / APNG / 动画 WebP）。真机上「卡不卡、
颜色对不对、有没有撕裂」这类问题，用**图案本身有明确预期**的素材一眼就能看出来 ——
所以这里造的每个文件都带一个"看什么"的目的，见 README.md 里的表。

尺寸按真屏来：
  · AXS15352 —— 240×296（4 线 SPI + DC）
  · ST77916  —— 360×360（QSPI 圆屏）
页面里的「铺满 / 适应」会缩放，但按原生尺寸生成最不容易糊。

依赖：Pillow（画帧）+ numpy（渐变/棋盘）+ imageio-ffmpeg（自带 ffmpeg 7.1，编 MP4/WebM）。
本机已装；换机器：
    python -m pip install pillow numpy imageio-ffmpeg

用法：
    python tools/dev/make-anim-samples.py                 # 写到 samples/anim/
    python tools/dev/make-anim-samples.py --out some/dir  # 换目录
    make samples-anim                                     # 同第一条
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import imageio_ffmpeg
except ImportError:                                             # pragma: no cover
    imageio_ffmpeg = None

FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}
# 帧上要烧中文字（"彩条+横扫"这种一眼认得出的说明），Pillow 自带的默认字体没有 CJK 字形，
# 会画成一排豆腐块 —— 优先找系统里的中文字体，找不到就退回默认字体并只用 ASCII。
CJK_FONTS = [
    r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyhl.ttc', r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\Deng.ttf', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/System/Library/Fonts/PingFang.ttc',
]


def _pick_font(size: int):
    for p in CJK_FONTS:
        try:
            return ImageFont.truetype(p, size), True
        except OSError:
            continue
    return ImageFont.load_default(size=size), False


def font(size: int):
    if size not in FONT_CACHE:
        FONT_CACHE[size] = _pick_font(size)
    return FONT_CACHE[size]


def label(d: ImageDraw.ImageDraw, xy, text, size=14, fill=(255, 255, 255), bg=(0, 0, 0)):
    """左下角那种"带底衬的小字"，保证任何背景上都读得清。"""
    f, cjk = font(size)
    if not cjk:
        text = text.encode('ascii', 'replace').decode('ascii')
    box = d.textbbox(xy, text, font=f)
    pad = 3
    if bg is not None:
        d.rectangle((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad), fill=bg)
    d.text(xy, text, font=f, fill=fill)


# --------------------------------------------------------------------------- 帧生成

def frames_bars_sweep(w, h, n=50):
    """彩条 + 横扫白线：查**颜色顺序 / 渐变台阶 / 有没有整列错位**。

    上 70% 是 8 条 75% 彩条，下 30% 是 11 级灰阶；一根 3px 白线每帧右移，
    走到头回卷 —— 白线必须笔直、不能有锯齿或撕裂，灰阶必须能数出 11 级。
    """
    bars = [(191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
            (191, 0, 191), (191, 0, 0), (0, 0, 191), (0, 0, 0)]
    bw = w / len(bars)
    split = int(h * 0.7)
    steps = [(round(i * 255 / 10),) * 3 for i in range(11)]
    sw = w / len(steps)
    for i in range(n):
        im = Image.new('RGB', (w, h))
        d = ImageDraw.Draw(im)
        for k, c in enumerate(bars):
            d.rectangle((k * bw, 0, (k + 1) * bw, split), fill=c)
        for k, c in enumerate(steps):
            d.rectangle((k * sw, split, (k + 1) * sw, h), fill=c)
        x = (i / n) * w
        d.rectangle((x, 0, x + 2, split), fill=(255, 255, 255))
        label(d, (6, h - 26), f'彩条+横扫 {i + 1}/{n}  {w}x{h}', 15)
        yield im


def frames_ball_grid(w, h, n=60):
    """网格 + 弹跳球（带拖尾）：查**运动是否均匀、丢帧会不会看出来**。

    球的位置是解析的（正弦弹跳），每帧位移 ~6px；拖尾用 8 个渐隐圆。
    丢帧时球会"跳格"，一眼能看出；撕裂时球的圆边会断开。
    """
    grid = Image.new('RGB', (w, h), (12, 16, 28))
    gd = ImageDraw.Draw(grid)
    for y in range(0, h, 20):
        gd.line((0, y, w, y), fill=(30, 40, 60))
    for x in range(0, w, 20):
        gd.line((x, 0, x, h), fill=(30, 40, 60))
    r = 15
    span_x, span_y = w - 2 * r - 2, h - 2 * r - 2
    for i in range(n):
        im = grid.copy()
        d = ImageDraw.Draw(im)
        t = i / n
        bx = 1 + r + span_x * abs(((t * 2) % 2) - 1)
        by = 1 + r + span_y * abs(((t * 3) % 2) - 1)
        for k in range(8, 0, -1):
            a = k / 8
            px = 1 + r + span_x * abs((((t - a * 0.03) * 2) % 2) - 1)
            py = 1 + r + span_y * abs((((t - a * 0.03) * 3) % 2) - 1)
            col = int(40 + 60 * (1 - a))
            d.ellipse((px - r, py - r, px + r, py + r), fill=(col, col // 2, col // 3))
        d.ellipse((bx - r, by - r, bx + r, by + r), fill=(255, 64, 64), outline=(255, 220, 220))
        label(d, (6, h - 26), f'弹跳球 {i + 1}/{n}  丢帧=跳格', 15)
        yield im


def frames_rgb_ramp(w, h, n=36):
    """平移的色相/明度渐变 + 底部灰阶条：查**RGB565 的色深、字节序、R/B 是否反**。

    色相横向铺满、明度纵向渐变，每帧整体平移 —— 顺时针的彩虹应该平滑无台阶；
    底部 16 级灰阶里若出现偏色（发绿/发紫），就是字节序或 R/B 交换选错了。
    """
    yy = np.linspace(0.0, 1.0, h - 24, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    for i in range(n):
        hue = (xx + i / n) % 1.0
        r = np.clip(np.abs(hue * 6 - 3) - 1, 0, 1)
        g = np.clip(2 - np.abs(hue * 6 - 2), 0, 1)
        b = np.clip(2 - np.abs(hue * 6 - 4), 0, 1)
        val = 0.25 + 0.75 * yy
        rgb = (np.stack([r * val, g * val, b * val], -1) * 255).astype(np.uint8)
        gstep = np.repeat(np.arange(16, dtype=np.uint8) * 17, max(1, w // 16))[:w]
        strip = np.repeat(gstep[None, :, None], 24, 0).repeat(3, 2)
        im = Image.new('RGB', (w, h), (0, 0, 0))
        im.paste(Image.fromarray(rgb, 'RGB'), (0, 0))
        im.paste(Image.fromarray(strip, 'RGB'), (0, h - 24))
        d = ImageDraw.Draw(im)
        label(d, (6, h - 24), f'色相平移 {i + 1}/{n}（底部 16 级灰阶）', 14, bg=None)
        yield im


def _cube_edges():
    pts = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    edges = []
    for a in range(8):
        for b in range(a + 1, 8):
            if sum(1 for k in range(3) if pts[a][k] != pts[b][k]) == 1:
                edges.append((pts[a], pts[b]))
    return edges


def frames_cube_clock(w, h, n=100, fps=25):
    """旋转线框立方体 + 秒针 + 帧号：查**流畅度（fps 上限）与帧序**。

    立方体每帧转 1.2°，圆盘的秒针每 25 帧走一格（= 1 秒）；帧号连续递增。
    帧号跳号 = 丢帧；立方体一顿一顿 = 链路喂不满；边缘出现斜纹 = 撕裂。
    """
    edges = _cube_edges()
    cx, cy = w / 2, h / 2 - 12
    scale = min(w, h) * 0.28
    for i in range(n):
        im = Image.new('RGB', (w, h), (0, 0, 0))
        d = ImageDraw.Draw(im)
        t = i / fps
        ax, ay = t * 1.2, t * 0.71
        for p, q in edges:
            pr = []
            for pt in (p, q):
                x, y, z = pt
                x, z = x * math.cos(ax) - z * math.sin(ax), x * math.sin(ax) + z * math.cos(ax)
                y, z = y * math.cos(ay) - z * math.sin(ay), y * math.sin(ay) + z * math.cos(ay)
                k = 2.6 / (2.6 + z)
                pr.append((cx + x * scale * k, cy + y * scale * k))
            d.line((pr[0][0], pr[0][1], pr[1][0], pr[1][1]), fill=(80, 230, 255), width=2)
        rr = min(w, h) * 0.30
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(60, 60, 60), width=2)
        sec = (i % fps) / fps * 2 * math.pi - math.pi / 2
        d.line((cx, cy, cx + rr * math.cos(sec), cy + rr * math.sin(sec)), fill=(255, 210, 0), width=3)
        label(d, (8, h - 30), f'立方体 {w}x{h} {fps}fps', 16)
        label(d, (8, 8), f'frame {i + 1:03d} / {n:03d}', 22, fill=(255, 255, 255))
        yield im


def frames_checker_scroll(w, h, n=75):
    """斜向滚动的棋盘 + 边框 + 十字线：查**撕裂 / 卷屏 / 整屏刷新是否完整**。

    棋盘每帧斜移 4px（周期 32px，8 帧一个循环）；红色边框必须四边都在
    （缺一边 = 开窗少算了），中心十字必须始终居中（偏移 = 行列地址错位）。
    """
    cell = 16
    ys = np.arange(h)[:, None]
    xs = np.arange(w)[None, :]
    for i in range(n):
        off = i * 4
        board = (((ys + off) // cell + (xs + off) // cell) % 2).astype(np.uint8) * 255
        rgb = np.repeat(board[:, :, None], 3, 2)
        im = Image.fromarray(rgb, 'RGB')
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, w - 1, h - 1), outline=(255, 0, 0), width=2)
        d.line((w / 2, 0, w / 2, h), fill=(0, 128, 255))
        d.line((0, h / 2, w, h / 2), fill=(0, 128, 255))
        label(d, (8, h - 30), f'棋盘斜移 {i + 1}/{n}  {w}x{h}', 16)
        yield im


# --------------------------------------------------------------------------- 编码

def save_image_seq(frames: list[Image.Image], path: Path, dur_ms: int, fmt: str, **kw):
    frames[0].save(path, format=fmt, save_all=True, append_images=frames[1:],
                   duration=dur_ms, loop=0, **kw)


def save_video(frames: list[Image.Image], path: Path, fps: int, codec: str, extra: list[str]):
    """rawvideo 进 ffmpeg 的 stdin —— 比任何封装库都好控制，出问题一眼看得见命令行。"""
    if imageio_ffmpeg is None:
        raise RuntimeError('没装 imageio-ffmpeg，编不了 MP4/WebM：python -m pip install imageio-ffmpeg')
    w, h = frames[0].size
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-hide_banner', '-loglevel', 'error',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps), '-i', '-',
           '-an', '-c:v', codec, *extra, '-pix_fmt', 'yuv420p', str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for im in frames:
            p.stdin.write(im.tobytes())
    finally:
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError('ffmpeg 编码失败：' + ' '.join(cmd))


# --------------------------------------------------------------------------- main

def gif_colors(frames, colors):
    """GIF 只有 256 色调色板：先量化再编码，体积能小一半（灰阶/彩条本来就没几个色）。"""
    return [f.quantize(colors=colors, method=Image.MEDIANCUT).convert('P', palette=Image.ADAPTIVE, colors=colors)
            for f in frames]


def main(argv=None):
    ap = argparse.ArgumentParser(description='造屏页动画/视频示例素材')
    ap.add_argument('--out', default='samples/anim', help='输出目录（默认 samples/anim）')
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    made = []

    def note(name, what):
        p = out / name
        made.append((name, p.stat().st_size, what))
        print(f'  {name:28s} {p.stat().st_size / 1024:8.1f} KB  {what}')

    print(f'写到 {out.resolve()}')

    # ① 彩条 + 横扫（GIF · 240×296）
    f = list(frames_bars_sweep(240, 296, 50))
    save_image_seq(gif_colors(f, 16), out / 'bars-sweep-240x296.gif', 40, 'GIF')
    note('bars-sweep-240x296.gif', 'GIF · 彩条+横扫白线 · 查颜色顺序/灰阶')

    # ② 弹跳球（GIF · 240×296）
    f = list(frames_ball_grid(240, 296, 60))
    save_image_seq(gif_colors(f, 32), out / 'ball-grid-240x296.gif', 40, 'GIF')
    note('ball-grid-240x296.gif', 'GIF · 网格弹跳球 · 查运动均匀/丢帧')

    # ③ 色相平移（动画 WebP · 240×296，无损）
    f = list(frames_rgb_ramp(240, 296, 36))
    save_image_seq(f, out / 'rgb-ramp-240x296.webp', 40, 'WEBP', lossless=True, quality=100, method=4)
    note('rgb-ramp-240x296.webp', '动画 WebP · 色相/明度平移 · 查 RGB565 色深')

    # ④ 帧号计数（APNG · 240×296，无损）
    f = list(frames_cube_clock(240, 296, 50, 25))
    save_image_seq(f, out / 'count-cube-240x296.apng', 40, 'PNG')
    note('count-cube-240x296.apng', 'APNG · 帧号+秒针+立方体 · 查帧序/丢帧')

    # ⑤ 棋盘斜移（WebM / VP9 · 360×360）
    #    🚨 `-fflags/-flags +bitexact` 是**为了可复现**：matroska 封装默认会写 DateUTC（当前时间）
    #    之类的元数据，同一份帧跑两遍字节就不一样 —— 那样每次 `make samples-anim` 都会在 git 里
    #    显示"素材被改了"（2026-09-30 实测踩到；MP4 那路天生确定）。`-threads 1` 顺手也钉住，
    #    免得将来 VP9 多线程再引入不确定性。
    f = list(frames_checker_scroll(360, 360, 75))
    save_video(f, out / 'checker-scroll-360x360.webm', 25, 'libvpx-vp9',
               ['-b:v', '0', '-crf', '34', '-row-mt', '0', '-threads', '1', '-cpu-used', '4',
                '-fflags', '+bitexact', '-flags', '+bitexact'])
    note('checker-scroll-360x360.webm', 'WebM/VP9 · 棋盘斜移+边框十字 · 查撕裂')

    # ⑥ 立方体 + 秒针（MP4 / H.264 · 360×360，4 秒）
    f = list(frames_cube_clock(360, 360, 100, 25))
    save_video(f, out / 'cube-clock-360x360.mp4', 25, 'libx264',
               ['-preset', 'veryfast', '-crf', '23', '-movflags', '+faststart'])
    note('cube-clock-360x360.mp4', 'MP4/H.264 · 4 秒立方体+秒针+帧号 · 查流畅度')

    total = sum(s for _, s, _ in made)
    print(f'\n合计 {len(made)} 个文件 · {total / 1048576:.2f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
