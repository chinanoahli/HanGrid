# Han Grid 实现与开发记录

面向维护者。安装、构建参数和常见操作见 [README.md](README.md)。

## 构建与上游

- [CLI](han_grid/cli.py) 负责参数、工作区与输出目录的锁，以及构建、精简、Nerd 和打包的阶段编排。[构建任务](han_grid/build.py) 按字重、样式和地区调度进程。每个任务先生成 CFF2 OTF。请求 TTF 时，从本次 OTF 转换，两个格式都准备好后再发布。TTF-only 使用临时 OTF。
- [上游来源](han_grid/sources.py) 读取 [版本锁](han-grid.lock.json) 中的精确 Git 对象。显式更新优先选取 Sans 的发布标签，通过部分获取预取静态字体和许可证，核验所需地区和样式的文件与本地对象后更新锁。候选版本只含 Regular 样本时不足以支持完整构建。
- [基础合并](build_mono.py) 从新版 Sans 取 CJK，从旧 Mono 取西文与斜体。布局表按输出字形映射合并，保持半角、全角与非间距附标的度量。合并后声明固定字距并重算覆盖范围；TTF 转换 [build_ttf.py](build_ttf.py) 将三次曲线近似为二次曲线，核对映射、推进量、竖排原点及相关布局表。TTF 不生成 TrueType hint。
- [地区联合](han_grid/regions.py) 以官方地区子集的字符、变体和字形取并集，根据默认地区选择基础映射。汉字语言替换以对应官方子集为基准。若共享 CID 的码位需要不同地区替换，则为共享轮廓分配独立映射。联合差异的新增项以相同地区的新旧并集计算，已有字形和变体按地区比较。代表样式取本次实际构建的字重和样式，完整差异写为独立 JSON，字体记录保留摘要。
- `build_mono.py`、`build_ttf.py` 和 [repair_nerd_mono.py](repair_nerd_mono.py) 兼作兼容入口。当前旧入口新生成的基础字体写入构建身份。已识别的早期无身份地区子集在 [身份验证](han_grid/validation.py) 中从样式字段恢复字重和斜体。

## Windows 精简与盲文

- [参考字体提取](han_grid/windows.py) 读取所有 Unicode cmap 子表。每个集合成员解包为独立字体并回读核对码位和 UVS，随后立即释放解包文件。集合文件的摘要每个文件计算一次。地区并集去重排序，随附目录的 [catalog.json](han_grid/data/windows-cjk/catalog.json) 保留范围、变体和各成员来源摘要。
- [精简](han_grid/trimming.py) 按随附 Unicode 17 区段划分 CJK。保留同批有效地区组合的共用白名单，按 cmap 子表过滤基础映射，按基础码位保留 UVS，依赖布局闭包保住地区替换、竖排和必要组件。CFF2 精简复用一次轮廓边界遍历更新多个度量表。接近容量上限且启用精简时先过滤 CJK，为宽度分离和盲文留出字形槽位。
- [盲文](han_grid/braille.py) 以八位掩码绘制 256 个独立图案，包括空白 U+2800。生成时验证点数、轮廓区别和单格宽度。`gapless` 使用 `hhea` 基线间距分配四行点块，保持横向 500 单位。

## Nerd、校验和发布

- [补丁流程](han_grid/nerd.py) 解析 FontForge 可执行文件路径后才切换工作目录。Patcher 的 ZIP 缓存按内容寻址，预检缓存关联 Patcher 资源、FontForge 和字符覆盖。[预检脚本](han_grid/fontforge_probe.py) 计算启用的图标复制数和控制字形预留，形成容量上界；未精简的全集在预检前退出。
- 加工使用 `--complete --careful`，校验原有普通文本字符的推进量、发生码位冲突的轮廓边界、变体和新增图标。修复脚本按字节更新 `post.isFixedPitch`，单独检查其余表及 `head.checkSumAdjustment`。FontForge 生成的空白未映射控制字形可保留 333 的推进量。最终重新应用所选盲文设计并写入 NF 名称。
- [OTF 兼容转换](han_grid/outlines.py) 通过 name-keyed CFF1 让 FontForge 读取静态 CFF2，再恢复 CFF2。此转换移除原始 CFF hint。恢复 `VORG` 时用源字符和变体映射、字形名匹配可定位的记录，不能安全恢复时中止。对比竖排位置需要看原点和实际偏移，不能只比推进量。
- [读写与清单](han_grid/io.py) 在私有 `meta/hgrd` 保存家族、地区、样式、来源和处理记录。清单中的路径与 SHA-256 用于核对批量输入。输出先在临时路径校验，再替换目标。替换前关闭惰性读取器；路径规划核对输出边界、已有目标的文件身份及冲突。工作区和输出目录各有操作系统锁。
- [字体集](han_grid/collections.py) 先暂存各组并逐成员回读，核对名称、度量、字符映射与二进制字体表。允许集合封装导致的 `head` 校验值差异。全部暂存通过后发布，清单记录相对路径及混用提示。独立打包复用能取得的随附许可证。[文件命名](han_grid/naming.py) 与字体家族身份分离。
- [字形几何检查](han_grid/geometry.py) 遍历最终独立字体的所有轮廓，包括变体、连字和未编码字形。新构建的窄弯引号和选择符变体执行字格硬限制，独立加工的旧输入保留兼容性。其他有明显伸出或 Unicode 宽度异常的字形记录在清单，零推进的墨迹单独统计。Nerd 后处理按实际墨迹范围调整新增私用区图标的推进量。已有字符和共用基础字形受到保护，Powerline 符号的小幅伸出作为拼接例外。

## 开发中发现的回归

下表以可读的源码、测试或检查脚本作为参考。验收字体可按 README 所列命令重新生成。

| 曾发现的问题 | 当前处理及可复查依据 |
| --- | --- |
| `hmtx` 合规的字体，默认排版仍可能偏离字格 | 移除 Sans 的默认横向 `kern`。以 `あて` 检验实际推进量。将半角韩文与普通形式分配不同宽度，非间距长音符归零并移回基字上方。见 [合并逻辑](build_mono.py) 和 [HarfBuzz 检查](scripts/check_grid_shaping.py)。 |
| 弯引号在终端按一格排版时，左引号的墨迹落进下一格 | 默认 cmap 选用 U+FE00 的窄体轮廓。把依赖旧 cmap 的 UVS 转为显式字形，让 `fwid` 继续选用 U+FE01 全角轮廓，并在原有竖排查找表中添加窄体别名。用 `--quotes-only` 核对四种引号的墨迹、推进量和宽度变体。见 [合并逻辑](build_mono.py) 和 [HarfBuzz 检查](scripts/check_grid_shaping.py)。 |
| 联合子集直接继承默认地区的替换，日文 `隣` 与官方 JP 子集不同 | 从对应官方子集建立汉字替换。地区并集比较以旧 SC 与旧 JP 为共同基准，避免将既有 JP 变体计为升级新增。见 [地区逻辑](han_grid/regions.py) 和 [地区测试](tests/test_han_grid.py)。 |
| 基础字体盲文缺字，Nerd 默认覆盖为矩形，满点盲文跨行留白 | 构建及 Nerd 后处理统一应用盲文设计，更新 OS/2 Unicode 覆盖声明。无间隙模式以默认基线间距测试。见 [盲文测试](tests/test_windows_trim.py)。 |
| 等宽标志丢失、Nerd 图标替换心形后占位变化、Nerd OTF 标点竖排错位 | 分别恢复固定字距标志、保护普通文本码位、恢复竖排原点。请用对应的 [CoreText 检查](scripts/check_coretext_mono.swift) 和 [排版检查](scripts/check_grid_shaping.py) 复验。 |
| 精简曾遗漏非首选 Unicode 子表，或在不区分大小写的卷覆盖输入 | 回归测试覆盖次要子表和独立 UVS。规划阶段统一扩展名，并对已有目标比较真实文件身份。见 [精简测试](tests/test_windows_trim.py) 和 [目标检查](han_grid/io.py)。 |
| 旧字体身份缺少样式、FontForge 相对路径失效、读句柄未关闭即替换 | 在下游命名之前补齐已识别的旧身份。进程启动前解析路径，原子替换前关闭读取器。见 [身份验证](han_grid/validation.py) 和 [补丁流程](han_grid/nerd.py)。 |
| 分组打包后续失败造成旧清单摘要失效，移动目录后路径失效 | 先暂存全部分组，成功后更新目标与清单。版本或盲文设计混用仅提示，路径相对于清单。见 [打包测试](tests/test_han_grid.py)。 |

macOS 验收以单元测试、CoreText 和 HarfBuzz 为依据。改变布局或转换后应同时验证基础与 NF 的 OTF、TTF；集合验证可重用独立字体结果作对照。Windows 和 Linux 原生文件替换、平台字体筛选和渲染仍需实机验收。

弯引号修复在 macOS 上通过了 33 项单元测试。SC+KR Windows 精简版的 Regular 和 Regular Italic OTF／TTF 通过 HarfBuzz 边界检查与 CoreText 等宽检查。SC+KR Nerd TTF 与 JP Nerd OTF 也通过引号检查。简中竖排的 `“你好”` 和 `‘你好’` 在修复前后有相同的轮廓和位置。复验命令见 [README.md](README.md)。

全字形检查与 Nerd 图标宽度调整通过了 35 项单元测试。SC+JP Regular 的基础 OTF／TTF 各检查 41,407 个字形，Windows 精简 OTF 检查 41,155 个字形。两种 Nerd 格式各有 10,175 个图标调整为双格。HarfBuzz 验证齿轮、文件夹和宽版 Powerline 图标推进 1000，窄版 Powerline 三角推进 500。CoreText 仍识别两种 Nerd 格式为等宽。
