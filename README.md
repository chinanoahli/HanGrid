# Han Grid

我是 CJK 字体用户，主要用 CJ 两种语言，同时略有一点强迫症和完美主义。但是即使我再怎么找，也很难找到有正好完美符合下列需求的字体。此前断断续续地研究了很久字体制作，但因为影响字体观感的参数过多，调试需要耗费大量精力，因此并未觅得良方。最近萌生了利用 AI 协助重新尝试的想法，因此催生了此项目。最终消耗了约两周 GPT Pro 额度的用量，完成了我心中完美字体的制作。

我心中的完美字体:

+ 支持尽量多的 CJK 字符，包含两种繁体字形
+ 严格的等宽设定，2 个半角英数字符刚好等于 1 个 CJK 全角字符，而且能被操作系统正确识别为 `等宽字体` 以兼容部分只能使用等宽字体的软件
+ 有好看的终端模拟器显示效果(绝不混淆编码人员友好的英数字符设计，以及加入 Nerd Fonts 图标)

我层尝试过使用 [Source Han Mono / 思源等宽](https://github.com/adobe-fonts/source-han-mono)，它结合了 [Source Code Pro](https://github.com/adobe-fonts/source-code-pro/) 和 [Source Han Sans / 思源黑体](https://github.com/adobe-fonts/source-han-sans/)，这两个字体都是我喜欢的字体，它也基本上满足了前 2 点要求。

但它有一个致命的问题，虽然它名字里面带有 `Mono / 等宽`，但实际上元数据设置有问题，导致其无法被操作系统正确地识别为等宽字体，因此一些软件中根本无法选择使用；其次，它的 CJK 字形版本已经 7 年没更新过了(对于本项目发布的时间 2026-11-25 来说)！因此它存在着无法忽视的巨大问题。

我也曾尝试使用 [Sarasa Gothic / 更纱黑体](https://github.com/be5invis/Sarasa-Gothic)。这是最贴近我上面三条要求的字体，但是它也有很明显的缺点，满足我前 2 点要求的字体的文件体积实在是太大了(超过 500M)，这很难让人接受，而且它并不满足第 3 点要求。

## 项目简述

Han Grid 是构建 Han Grid Mono 的 Python 命令行工具。它将 Source Han Sans 的新版 CJK 字形和地区排版与 Source Han Mono 的西文及斜体结合，面向终端提供单格 500、双格 1000 的字宽和系统等宽标记。组合附标与基字共用一格。

工具支持 CJK 全集和 SC、TW、HK、JP、KR 地区子集，可合并多个地区，并按需启用 Windows 字符精简、Nerd Fonts 加工和字体集打包。[实现细节与开发记录](AGENTS.md)

## 安装

需要 Python 3.10+ 和 Git。首次使用仓库内的上游锁定版本时，初始化子模块。

```sh
git submodule update --init
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
han-grid --help
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活。也可通过 `python -m han_grid` 调用。Nerd 加工另需支持 Python、`fontforge` 和 `psMat` 的 FontForge。

## 构建字体

```sh
han-grid build
han-grid build --languages sc jp --default-region jp --format both --family-name "Example Mono"
han-grid build --languages kr --format ttf --weights Regular Bold --styles upright
han-grid build --full --regions sc jp
```

默认生成 SC 子集的七个字重及直立、斜体，共 14 个独立 OTF。默认家族名为 `Han Grid Mono`，字体版本为 `1.102`，输出到工作区的 `dist/han-grid/`。TTF 和同时生成两种格式分别使用 `--format ttf` 和 `--format both`。

`--languages` 生成所选地区的一个联合子集。组合含 SC 时默认采用 SC 字形，其余情况下默认采用第一个地区。`--default-region` 可从所选地区中改选。地区子集沿用上游定义，SC 也可能包含部分繁体字、假名和注音。

`--full` 为五个地区分别构建全覆盖版本。`--regions` 可筛选默认显示地区。其他常用参数包括 `--weights`、`--styles upright|italic|both`、`--jobs`、`--version` 和 `--output`。

弯引号 `‘ ’ “ ”` 默认使用一格宽度，适配将这些字符按单列排版的终端。支持变体选择符或 OpenType 功能的应用可用 U+FE01 或 `fwid` 选择全角字形。变体选择符本身不会改变终端的列宽计算。

## Windows CJK 精简

```sh
han-grid build --languages sc jp --trim-windows
han-grid build --full --regions sc --trim-windows --collection
han-grid trim path/to/base.otf --output dist/trimmed
han-grid trim path/to/build-manifest.json --nerd --collection --output dist/trimmed
```

精简仅过滤 CJK 字符，保留非 CJK 字符、获准基础字符的现有变体和必要的排版字形。单地区使用对应清单，地区组合使用所选清单的并集，全集使用四份清单的并集。HK 参考 TW。独立 `trim` 接受基础字体或其清单，保留输入文件。精简在 Nerd 补丁前执行。

工具随附编码清单，可直接精简。重新提取时，将自己的参考字体按 `zh-cn`、`zh-tw`、`ja-jp`、`ko-kr` 分目录放置。工具读取各地区所有 OTF、TTF、OTC 和 TTC 成员。

```sh
han-grid extract-windows --source Tools/WindowsLocalizedFonts --output data/windows-cjk
han-grid build --trim-windows --windows-catalog data/windows-cjk
```

省略参数时，提取默认使用上述目录，构建优先读取工作区的 `data/windows-cjk/`，其次使用随附清单。

## 盲文

```sh
han-grid build --braille circle
han-grid build --braille rectangle
han-grid build --braille gapless
han-grid build --braille preserve
```

默认生成 256 种单格圆点盲文。`rectangle` 改用矩形点，`gapless` 让点块在字体默认行距下横纵相接，`preserve` 保留该阶段输入或 Patcher 生成的设计。Nerd 加工也支持 `--braille`。应用自定义行距时，实际拼接以应用使用的行距为准。

## Nerd Fonts 和字体集

自行准备本地 FontPatcher，放入 `Tools/FontPatcher/` 或 `Tools/FontPatcher.zip`。可用 `--patcher` 指定本地目录或 ZIP，并用 `--fontforge` 指定可执行文件。

```sh
han-grid build --languages sc jp --format both --nerd --collection
han-grid nerd path/to/base.ttf --output dist/patched
han-grid collect path/to/build-manifest.json --output dist/release
```

Nerd 加工保留已有普通文本字符及其字宽。未经精简的全集须先精简才能加工，精简后的全集与地区子集都受每个字体 65,535 个字形的上限约束。测试可使用 `--skip-nerd-glyph-check` 跳过容量预检，最终字体仍需通过校验。容量不足时可减少地区。

新增 Nerd 私用区图标按墨迹范围使用 500 或 1000 单位的推进量。Powerline 拼接符号保留少量用于贴合相邻字格的伸出。终端若按 Unicode 自行给私用区字符分配单列，显示仍取决于终端的列宽规则。

`--collection` 或独立的 `collect` 将 TTF 打包为 TTC、OTF 打包为 OTC。基础字体与 Nerd 版本分别打包，独立文件继续保留。成员版本或盲文设计不同会提示并继续。重复的 PostScript 名称会报错。

文件名格式为 `FamilyName-字重样式-地区组合[-WinTrim][-NF].扩展名`。全集使用 `CJK`，字体集用 `Collection` 代替字重样式。字体菜单保留指定的家族名，Nerd 版本追加 `NF`。如需同时安装不同地区的同样式字体，请为它们指定不同家族名。

## 更新与输出

```sh
han-grid update
han-grid update --sans-version 2.005R --mono-version 1.002
han-grid --workspace /path/to/project build --languages sc jp
```

普通构建使用 [版本锁](han-grid.lock.json)。需要切换上游版本时运行 `update`，再重新构建。构建清单会指向上游差异报告，便于查阅新增字符与各地区字形变化。

构建、精简、Nerd 加工和打包各写一份清单，记录文件路径、完整性摘要及失败项。发布目录可连同清单和许可证一起移动。运行中间产物、字体和本地参考字体由 Git 忽略。工具空闲时可清理缓存和日志，上游对象清除后需重新获取锁定来源。

构建、精简和 Nerd 加工会检查每个字形的横向墨迹和推进量。清单中的 `glyph_geometry` 记录检查数量、越界示例和 Nerd 图标宽度统计。新构建的弯引号超出字格会中止发布。斜体和附标有正常的伸出情况，其他可疑边界留在清单中供复核。

## 兼容性与检查

macOS 可用 CoreText 检查等宽标记。Windows 和 Linux 的完整构建与显示流程仍待实机验收。HarfBuzz 脚本用于检查 SC+JP Regular 的基础与 NF 产物，需安装 `hb-shape`。

```sh
python -m unittest discover -s tests -v
swift scripts/check_coretext_mono.swift path/to/generated-font.otf
python scripts/check_grid_shaping.py path/to/generated-sc-jp-regular.otf
python scripts/check_grid_shaping.py --quotes-only path/to/generated-font.otf
```
