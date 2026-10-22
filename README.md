# 文件内容合集生成工具

把多个文件夹/文件里的内容汇总成一份 txt + pdf 报告，带图形界面，支持拖拽添加、自定义排除规则、自动记住上次的条目，同时支持命令行参数无界面静默运行。

## 如何获取 exe（无需本地装 Python / Windows 电脑）

1. 把本仓库所有文件上传到你自己的 GitHub 仓库（保持目录结构不变，`.github/workflows/build-exe.yml` 这个路径必须保留）。
2. 打开仓库的 **Actions** 标签页：
   - 修改并推送 `files_report_gui.py` 会自动触发打包；
   - 如果没有自动触发，点击左侧的 "打包为 Windows exe" workflow，再点右侧 "Run workflow" 手动触发一次。
3. 等待运行完成（一般 1-2 分钟），点进这次运行记录，底部 **Artifacts** 里下载 `collectcode`。
4. 解压后得到 `collectcode.exe`，双击运行即可，绿色便携版，不依赖本机 Python 环境。

## 使用说明

### 1. 图形界面模式（默认）
直接双击运行 `collectcode.exe`：
- **添加文件/文件夹**：点按钮多次添加，或直接把文件/文件夹从资源管理器拖进左侧列表框。
- **限制条目**：右侧输入框添加排除规则，支持通配符，如 `*.log`、`node_modules`、`.git`。
- **自动保存配置**：所有条目和规则会自动保存到 exe 同目录下的 `config.json`，下次打开自动加载。
- **固定文件覆盖生成**：点击 **"生成合集"** 或 **"生成合集并复制到剪贴板"** 后，会在 **exe 所在目录** 直接生成并覆盖固定的 `combined_files_report.txt` 和 `combined_files_report.pdf`，不会产生多余的历史文件。

### 2. 命令行 / 快捷方式无界面模式（CLI）
在已配置好 `config.json` 的前提下，可以通过命令行、`.bat` 脚本或桌面快捷方式带参数启动程序。此时**不会弹出图形界面**，程序在后台完成对应动作后会自动退出：

| 启动参数 | 简写 | 对应功能 |
| :--- | :--- | :--- |
| `collectcode.exe --gen` | `collectcode.exe -g` | 静默读取配置，生成并覆盖 `combined_files_report.txt` 和 `.pdf` 后退出 |
| `collectcode.exe --copy` | `collectcode.exe -c` | 静默读取配置，生成并覆盖报告文件，同时将汇总文本**复制到系统剪贴板**后退出 |

## 本地直接用 Python 运行（可选）

```bash
pip install -r requirements.txt

# 启动图形界面
python files_report_gui.py

# 无界面直接生成合集
python files_report_gui.py --gen

# 无界面生成合集并复制到剪贴板
python files_report_gui.py --copy