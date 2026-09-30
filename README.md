# 文件内容合集生成工具

把多个文件夹/文件里的内容汇总成一份 txt + pdf 报告，带图形界面，支持拖拽添加、自定义排除规则、自动记住上次的条目。

## 如何获取 exe（无需本地装 Python / Windows 电脑）

1. 把本仓库所有文件上传到你自己的 GitHub 仓库（保持目录结构不变，`.github/workflows/build-exe.yml` 这个路径必须保留）。
2. 打开仓库的 **Actions** 标签页：
   - 如果没有自动触发，点击左侧的 "打包为 Windows exe" workflow，再点右侧 "Run workflow" 手动触发一次。
3. 等待运行完成（一般 1-2 分钟），点进这次运行记录，底部 **Artifacts** 里下载 `文件内容合集工具-exe.zip`。
4. 解压后得到 `文件内容合集工具.exe`，双击运行即可，绿色便携版，不依赖本机 Python 环境。

## 使用说明

- **添加文件/文件夹**：点按钮多次添加，或直接把文件/文件夹从资源管理器拖进左侧列表框。
- **限制条目**：右侧输入框添加排除规则，支持通配符，如 `*.log`、`node_modules`、`.git`。
- 所有条目和规则会自动保存到 exe 同目录下的 `config.json`，下次打开自动加载。
- 点击"生成合集"后，会在 **exe 所在目录** 生成带时间戳的 `combined_files_report_*.txt` 和 `.pdf`。

## 本地直接用 Python 运行（可选）

```bash
pip install -r requirements.txt
python files_report_gui.py
```
