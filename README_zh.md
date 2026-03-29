# kckfxgen

**Kindle Comics KFX Gen**

[English README](README.md)

将 **EPUB** 或 **漫画压缩包**（ZIP / CBZ / RAR / CBR）转为 Kindle 固定版式漫画用的 **KFX**，**不再依赖 Kindle Previewer**。流程：生成本工具自己的 **`.kpf`**（内含 `book.kdf`），再用与 [calibre-kfx-output](https://github.com/dstaley/calibre-kfx-output) 中 [`kfx_container.py`](https://github.com/dstaley/calibre-kfx-output/blob/master/kfxlib/kfx_container.py) 相同的 **`KfxContainer.serialize`** 路径打成单文件 **`.kfx`**（`KpfContainer` 读库 → `KpfBook` 修正 → 序列化）。对应实现已直接纳入仓库目录 **`kckfxgen/kfxlib/`**（GPL v3，原作者 John Howell 等；许可证全文见 **`kckfxgen/kfxlib/COPYING`**）。

与基于 MOBI 的漫画相比，KFX 漫画通常在大体积、高分辨率图源下**翻页与加载更快**，设备上**更不容易卡死**；固定版式下**版面更规整**，例如**居中对齐、减少多余白边**，阅读体验更丝滑。生成 KDF 时会按**每张图在应用 EXIF 方向后的宽高**写入 **`orientation`（portrait / landscape）**，并在图书元数据中声明同时支持竖屏与横屏，便于混排竖页、横页。

每次成功转换会在目标目录生成 **`{安全主名}_{随机}.kfx`**；中间 **`.kpf`** 仅存在于临时目录，结束后删除。

## 依赖

- **Python 3.10+**
- **必需（pip）**：`amazon-ion`、`lxml`、`pillow`
- **Python 自带** `sqlite3` 所链接的 **SQLite ≥ 3.8.2**（`book.kdf` 使用 `WITHOUT ROWID`；常见 CPython 安装均满足）
- **可选**
  - **双页裁切**（`--split-spreads`）：`numpy`、`pillow`（先检测**正中空白中缝**再决定是否沿正中切开；并行）
  - **RAR / CBR**：`rarfile`，且系统须能调用 **UnRAR**（否则解压会失败）

```bash
pip install amazon-ion lxml pillow
# 若使用 --split-spreads（与基础依赖相同）：
pip install pillow
# 若处理 .rar / .cbr：
pip install rarfile
```

## 支持的输入

| 类型 | 扩展名 | 说明 |
|------|--------|------|
| EPUB | `.epub` | 按 spine / manifest 收集图片（与原先一致） |
| 漫画包 | `.zip`、`.cbz` | 标准库解压；包内图片按**路径自然序**排序 |
| 漫画包 | `.rar`、`.cbr` | 需 `rarfile` + UnRAR |

`path` 可为**上述任一文件**，或**包含这些文件的目录**（递归扫描、多文件时线程池并发）。

## 命令行参数

| 参数 | 说明 |
|------|------|
| `path` | 输入：单文件（`.epub` / `.zip` / `.cbz` / `.rar` / `.cbr`）或目录 |
| `-o` / `--output` | **仅单文件**：KFX 输出目录（默认：与输入文件同目录） |
| `--output-dir` | **批量**：所有 `.kfx` 写入同一目录（命名规则见上文）；不可与 `-o` 同用 |
| `-j` / `--jobs` | 并发线程数（默认约 `min(8, CPU 核心数)`） |
| `-d` / `--debug` | DEBUG 日志 |
| `--split-spreads` | 宽幅图（宽≥高×1.25）：仅当检出**空白装订中缝**时沿**几何正中**裁成两半并插入阅读顺序；否则保留整图（默认**关闭**） |
| `--split-page-order` | 与 `--split-spreads` 配合：`right-left`（默认，先右半再左半）或 `left-right`（先左后右） |
| `--rotate-landscape-90` | 写入 KDF 前将**横图**（宽 > 高）**逆时针旋转 90°** 以竖屏展示；竖图不变 |
| `--page-progression` | KPF / KDF **翻页方向**：`ltr`（默认，从左向右）或 `rtl`（从右向左，日漫常见）。同步写入 `book.kcb` 的 `book_reading_direction` 与 `book.kdf` 中 `document_data` 的 `direction` |
| `--title` | 覆盖**书名**（见下：漫画包默认识别；EPUB 则覆盖 OPF `dc:title`） |
| `--author` | 覆盖**作者**（漫画包：文件名中「 - 」右侧；EPUB 覆盖 `dc:creator`） |
| `--publisher` | 覆盖**出版社**（漫画包：文件名末尾 […] 或 (…) 内；EPUB 覆盖 `dc:publisher`） |

### 漫画包（ZIP / CBZ / RAR / CBR）文件名 → 元数据

对**无 OPF** 的压缩包，程序用**主文件名**（不含扩展名）自动填 `title` / `author` / `publisher`：

1. 若末尾有 **`[…]`**，括号内作为 **publisher**，去掉该段；否则若末尾有 **`(…)`**，其内容作为 **publisher**。
2. 剩余部分若含 **` — `**、**` – `** 或 **` - `**（两侧有空格的连字符），按**第一次出现**拆开：左侧为 **title**，右侧为 **author**。
3. 若没有上述分隔符，则整段作为 **title**，author、publisher（若未由括号得到）为空。

示例：`进击的巨人 - 谏山创 [讲谈社].cbz` → title=`进击的巨人`，author=`谏山创`，publisher=`讲谈社`。

**批量**处理目录时，若使用 `--title` / `--author` / `--publisher`，**同一组值会应用到该次扫描到的每一个文件**（适合统一改名前批量试转；若每本书不同请逐本调用或依赖文件名解析）。

## 用法示例

```bash
# 单个 EPUB → KFX 写入与 EPUB 同目录（默认）
python main.py 路径/漫画.epub

# 单个 CBZ / ZIP（书名/作者/出版社从文件名解析，见上文）
python main.py 路径/漫画.cbz

# 手动覆盖元数据（漫画包或 EPUB 均可）
python main.py 路径/漫画.zip --title "自定义书名" --author "某作者" --publisher "某社"

# 单个 EPUB → KFX 写入指定目录
python main.py 路径/漫画.epub -o 输出目录

# 目录：递归处理其下全部支持的文件（多线程）；每个文件的 KFX 默认在其所在目录
python main.py 路径/含漫画的文件夹

# 批量：所有输入的 KFX 均写入同一目录
python main.py 路径/含漫画的文件夹 --output-dir 输出目录

# 并发线程数
python main.py 路径/文件夹 -j 4

# 宽幅跨页：几何中心裁切 + 先右后左插入（日漫常见）
python main.py 路径/漫画.epub --split-spreads

# 裁切后改为先左页再右页
python main.py 路径/漫画.zip --split-spreads --split-page-order left-right

# 从右向左翻页（日漫式；KCB + KDF 一致）
python main.py 路径/漫画.cbz --page-progression rtl

# 调试日志
python main.py 路径/漫画.epub -d
```

### Python API

`kckfxgen.pipeline` 中的 `convert_to_kfx`、`epub_to_kpf`、`comic_archive_to_kpf`、`convert_epub_to_kfx` 可通过关键字参数 **`page_progression="ltr"`**（默认）或 **`page_progression="rtl"`** 控制相同行为。

从**项目根目录**执行上述命令。

## 独立双页检测工具（仅处理散图）

仓库内 `kckfxgen/spread_split.py` 也可单独对 PNG/JPEG 等做检测与裁切（不写 KFX）：

```bash
python -m kckfxgen.spread_split 某图.png -o 输出目录
# 或（需从仓库根目录，保证能 import kckfxgen）
python src/comic_spread_split.py 某图.png --dry-run
```

自测裁切逻辑：`python test/run_spread_split_tests.py`（`test/` 下需有按 `cut*` / `nocut*` 前缀命名的样图）。
