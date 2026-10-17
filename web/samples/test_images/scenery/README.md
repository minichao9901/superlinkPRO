# 示例素材：屏页「图片/图案刷屏」的风景照片

给 `#panel`（SPI/QSPI 屏页）的「图片/图案刷屏」用的**真实照片**：4 类各 2 张，两种屏各一套 = **16 个 24bpp BMP**。

为什么除了程序化图案还要放照片：纯色/棋盘/色条验的是**几何**（开窗、切片、对齐、色序），
它们说明不了"真实内容在 RGB565 上像不像"—— 肤色会不会发绿、天空的深蓝渐变会不会出色带、
细碎花瓣/树叶/浪花在 5-6-5 位色深下糊成什么样，只有照片能看出来。

| 分组 | 文件（两套几何同名） | 作者（来源页） | 许可 | 看什么 |
|---|---|---|---|---|
| 花朵绿树 | `flowers_wildflower_meadow.bmp` | [NPS Photo](https://commons.wikimedia.org/wiki/File:During_peak_bloom_a_colorful_combination_of_wildflowers_bloom_together%2C_including_Sitka_valerian%2C_lupine%2C_and_magenta_paintbrush._%2857071e16-bead-4548-b247-34de9d62c8c2%29.JPG) | Public domain | 高山野花甸：色彩多、花瓣细碎，看小花瓣会不会糊成一片 |
| 花朵绿树 | `greenery_forest_path.bmp` | [Zed Can77](https://commons.wikimedia.org/wiki/File:Forest_path_surrounded_by_green_trees.jpg) | [CC0](https://creativecommons.org/publicdomain/zero/1.0/deed.en) | 林间小路：大面积绿色 + 逆光树叶，看绿色阶调与细节 |
| 美女 | `beauty_portrait_outdoor.bmp` | [Anthony Ginsbrook](https://commons.wikimedia.org/wiki/File:Outdoor_portrait_%28Unsplash%29.jpg) | [CC0](https://creativecommons.org/publicdomain/zero/1.0/deed.en) | 街拍人像：肤色是 RGB565 最挑的地方（发绿 = 色序/字节序错） |
| 美女 | `beauty_portrait_umbrella.bmp` | [Dmitry Makeev](https://commons.wikimedia.org/wiki/File:Russia%2C_Moscow_Oblast._Young_woman_with_umbrella%2C_studio_portrait.jpg) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) | 棚拍人像：大面积纯蓝背景，色偏一眼可见 |
| 蓝天白云 | `sky_cumulus_clouds.bmp` | [Lance Vanlewen](https://commons.wikimedia.org/wiki/File:White_Cumulus_Clouds_against_Blue_Sky_%282%29.jpg) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) | 蓝天积云：深蓝渐变最容易出色带（RGB565 只有 5/6/5 位） |
| 蓝天白云 | `sky_sun_and_cloud.bmp` | [Lance Vanlewen](https://commons.wikimedia.org/wiki/File:White_Cumulus_Clouds_against_Blue_Sky_with_Sun_Shining.jpg) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) | 太阳 + 云：过曝高光与蓝天的边界，看白色偏不偏 |
| 大海 | `sea_turquoise_bay.bmp` | [Fabio Pani](https://commons.wikimedia.org/wiki/File:Turquoise_sea_at_San_Gemiliano_beach%2C_Tortol%C3%AC%2C_Sardinia%2C_Italy.jpg) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) | 青绿海面：水色的青→蓝过渡，看青绿偏不偏 |
| 大海 | `sea_tropical_beach.bmp` | [Vyacheslav Argenberg](https://commons.wikimedia.org/wiki/File:Malapascua_%28island%29%2C_Tropical_beach%2C_Turquoise_water%2C_Philippines.jpg) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 热带海滩：白沙滩 + 椰林 + 蓝天，三种亮部同时压 RGB565 |

## 怎么用

```
samples/test_images/scenery/
├─ axs15352/    240×296   ← AXS15352（4 线 SPI + DC，竖屏）
├─ st77916/     360×360   ← ST77916（QSPI 圆屏；四角会被圆边切掉）
└─ manifest.json          每张的来源/作者/许可/尺寸/sha256（自测就拿它对账）
```

屏页 → 「图片/图案刷屏」→ 几何选对应屏（`axs15352 240×296` / `st77916 360×360`）→ **选择文件…** →
挑该几何目录下的 BMP → **刷这一张**。摆放方式（铺满/适应、R/B 交换、字节序）与图案那条路完全一致，
所以照片和图案可以交叉着刷、互相印证。

三条口径，别踩：

1. **必须是 24bpp BI_RGB**（或 16bpp）：页面的 `parseBMP` 只吃 16/24bpp 非压缩 BMP，
   把 JPEG/PNG 改个后缀名成 `.bmp` 会在页面上直接报错。照片走有损压缩也会污染"颜色基准"，所以不放 JPEG。
2. **圆屏留意构图**：ST77916 四角被圆边切掉，所以这批图的裁剪焦点都往中间调过；
   用「铺满」时别把主体挤出圆外。
3. **看"像不像"而不是"准不准"**：相机的白平衡本身带色偏，照片**不能**当色偏/色序的唯一判据 ——
   那是纯色与色条的活。照片用来看真实内容在 RGB565 上的观感（肤色、天空、海水）。

> 目录根下另有一批程序化 BMP（纯色/棋盘/色条/渐变…），那批是随页面自测生成的图案；
> 本目录（`scenery/`）专门放照片，两者互补、互不覆盖。

## 重新生成

```powershell
make samples-scenery                                        # = python tools/dev/make-scenery-samples.py
python tools/dev/make-scenery-samples.py --list              # 只列来源表（不下载不生成）
python tools/dev/make-scenery-samples.py --only sea_turquoise_bay
python tools/dev/make-scenery-samples.py --out D:\some\dir   # 换输出目录
```

- 首次运行会从 Commons 下载原图（1600px 缩略图，共约 10 MB）缓存到 `%TEMP%\scenery-src`，之后离线可重跑。
- 需要代理时按**标准环境变量**来（脚本不写死代理）：`$env:HTTPS_PROXY = 'http://127.0.0.1:7890'`。
- 依赖：Pillow（`python -m pip install pillow`）。
- **可复现**：脚本只做 cover 裁剪 + LANCZOS 缩放（不调色、不加锐化），连跑两遍字节一致；
  `--manifest` 会把每张的 sha256 写进 `manifest.json`。
- 改素材要动 `tools/dev/make-scenery-samples.py` 里的 `SOURCES`（**唯一事实源**：作者/许可/来源页/裁剪焦点），
  并连带更新上面那张表与 `manifest.json`。

## 自测

```powershell
make test-scenery        # = node tools/selftest/scenery-samples.test.mjs（15 项，纯 Node、离线）
```

守三件事：目录与 manifest 结构齐（4 类 × 2 张 × 2 屏）、`app/spi/image.js` 的 `parseBMP` 真解得动且几何/像素数正确、
逐文件 sha256 与 manifest 对得上（谁把图悄悄换了一张，这里会红）。

## 许可与署名

8 张全部取自 **Wikimedia Commons**，许可为 公有领域 / CC0 / CC BY 4.0 / CC BY-SA 4.0 ——
**上表就是署名页**（作者 + 来源页链接 + 许可链接）。CC BY / CC BY-SA 的图再分发或改作时请保留同样的署名与许可声明；
本仓库对这批图片的加工仅限于按目标比例裁剪并缩放。
