# --- START OF FILE files_report_gui.py ---
# -*- coding: utf-8 -*-
# 依赖安装: python.exe Scripts\pip.exe install chardet reportlab tkinterdnd2
#
# 功能：
#   1. 多项目 .json 配置管理（界面支持切换/新建/重命名/删除，每个 .json 独立保存文件列表、右侧排除规则、大小限制、是否追加 content.txt）
#   2. 可选在每次合并所有文件末尾自动读取并追加同目录下的 content.txt
#   3. 完整合入 ai-execute-code 核心功能（VFS 内存预演、公共目录前缀着色、文件写入、智能 Git Commit、剥离指令提取纯对话文本）
#   4. 内置局域网核心 HTTP API 服务（默认 0.0.0.0:8124），支持无中间文件直接获取指定项目的全文件合集、项目管理、补丁解析与执行
#   5. 作为总启动入口，保留原有 UI 并实时显示核心 API 服务与 http_ui.py (8123端口) 状态，支持一键启动/关闭网页服务
#   6. 命令行参数支持：
#       --list-projects (或 -l)          : 获取所有项目的名称（读取所有 .json 文件名）
#       --project <名>  (或 -p <名>)     : 指定要操作的项目名称（默认使用 config 或第一个项目）
#       --with-content  (或 -w)          : 合并文件末尾追加 content.txt 内容
#       --stdout        (或 -s)          : 仅在内存合成指定项目的全文件合集并输出到标准输出（不生成 txt/pdf 中间文件）
#       --gen           (或 -g)          : 静默生成 txt + pdf 后退出
#       --copy          (或 -c)          : 静默生成 txt + pdf 并复制到剪贴板后退出
#       --txt-copy      (或 -t)          : 静默仅生成 txt 并复制到剪贴板后退出
#       --mem-copy      (或 -m)          : 不生成任何磁盘文件，直接合成文本复制到剪贴板后退出
#       --audit-clip    (或 -a)          : 读取当前剪贴板并弹出 ai-execute-code 补丁审核窗口
import os
import re
import sys
import json
import time
import shutil
import socket
import fnmatch
import pathlib
import urllib.parse
import urllib.request
import subprocess
import threading
import queue
import uuid   # 放在文件中，确保 PyInstaller 自动把内置 uuid 库打入 exe，供外部 http_ui.py 调用
import runpy  # 用于让打包后的 collectcode.exe 直接运行外部的 http_ui.py

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk, simpledialog

import chardet
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4

# 尝试引入拖拽支持库，没装的话不影响其它功能，只是不能拖拽
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ================= 默认配置与常量 =================
DEFAULT_MAX_FILE_SIZE = 1 * 1024 * 1024   # 1MB，超过此大小的文件会被截断
DEFAULT_TRUNCATE_SIZE = 50 * 1024         # 50KB，截取前面这么多字符
DEFAULT_EXCLUDES = ["__pycache__", ".git", ".vs", "node_modules", "*.exe", "*.dll"]

REPORT_TXT_NAME = "combined_files_report.txt"
REPORT_PDF_NAME = "combined_files_report.pdf"
CONTENT_TXT_NAME = "content.txt"

CORE_API_PORT = 8124   # 本程序核心 API 端口（供 http_ui.py 及其他本地 exe 调用）
WEB_UI_PORT = 8123     # http_ui.py 网页端端口

# 排除非项目配置的内部 json 文件名（防止误把其他程序的固定记录当成项目）
RESERVED_JSON_NAMES = {"package.json", "tsconfig.json", "chat_sessions.json"}
# =================================================


def get_app_dir():
    """获取 exe（或脚本）所在目录，配置文件和生成结果都放这里"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_lan_ip():
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ================= 多项目 .json 管理 =================

def sanitize_project_name(name: str) -> str:
    """清理项目名中的非法文件名字符"""
    name = name.strip()
    if name.lower().endswith(".json"):
        name = name[:-5].strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "default"


def get_project_json_path(project_name: str) -> str:
    safe_name = sanitize_project_name(project_name)
    return os.path.join(get_app_dir(), f"{safe_name}.json")


def list_project_names() -> list[str]:
    """读取目录下所有项目 .json 文件名（不含扩展名）"""
    app_dir = get_app_dir()
    names = []
    try:
        for fname in os.listdir(app_dir):
            if fname.lower().endswith(".json") and fname.lower() not in RESERVED_JSON_NAMES:
                full_p = os.path.join(app_dir, fname)
                if os.path.isfile(full_p):
                    names.append(os.path.splitext(fname)[0])
    except Exception:
        pass

    # 按名称排序，但让 config 排在最前（兼容习惯）
    names.sort(key=lambda x: (0 if x.lower() == "config" else 1, x.lower()))
    if not names:
        # 若没有任何 .json，自动初始化一个默认的 config.json
        save_project_config("config", [], DEFAULT_EXCLUDES.copy(), DEFAULT_MAX_FILE_SIZE, DEFAULT_TRUNCATE_SIZE, False)
        names = ["config"]
    return names


def load_project_config(project_name: str = "config") -> dict:
    """加载指定项目的 .json 配置"""
    default = {
        "entries": [],
        "excludes": DEFAULT_EXCLUDES.copy(),
        "max_file_size": DEFAULT_MAX_FILE_SIZE,
        "truncate_size": DEFAULT_TRUNCATE_SIZE,
        "append_content_txt": False,
    }
    path = get_project_json_path(project_name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_project_config(
    project_name: str,
    entries: list,
    excludes: list,
    max_file_size: int,
    truncate_size: int,
    append_content_txt: bool = False,
) -> bool:
    """保存指定项目的配置到对应的 .json 文件"""
    data = {
        "entries": entries,
        "excludes": excludes,
        "max_file_size": int(max_file_size),
        "truncate_size": int(truncate_size),
        "append_content_txt": bool(append_content_txt),
    }
    path = get_project_json_path(project_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存配置失败 ({path}): {e}")
        return False


def delete_project_config(project_name: str) -> bool:
    path = get_project_json_path(project_name)
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def rename_project_config(old_name: str, new_name: str) -> tuple[bool, str]:
    old_safe = sanitize_project_name(old_name)
    new_safe = sanitize_project_name(new_name)
    if not new_safe:
        return False, "新项目名称不能为空"
    old_path = get_project_json_path(old_safe)
    new_path = get_project_json_path(new_safe)
    if not os.path.exists(old_path):
        return False, f"原项目不存在: {old_safe}"
    if old_safe.lower() != new_safe.lower() and os.path.exists(new_path):
        return False, f"目标项目名称已存在: {new_safe}"
    try:
        os.rename(old_path, new_path)
        return True, new_safe
    except Exception as e:
        return False, str(e)


# ================= content.txt 读取与写入 =================

def get_content_txt_path() -> str:
    return os.path.join(get_app_dir(), CONTENT_TXT_NAME)


def read_content_txt() -> str:
    """读取 exe 同目录下的 content.txt 内容"""
    p = get_content_txt_path()
    if not os.path.exists(p):
        return ""
    try:
        raw = pathlib.Path(p).read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def write_content_txt(text: str) -> bool:
    p = get_content_txt_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


# ================= 文件遍历与合集生成 =================

def is_excluded(name, rel_path, patterns):
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel_path, p):
            return True
    return False


def iter_files_from_entries(entries, exclude_patterns):
    """把混合的文件/文件夹条目列表，展开成 (来源标签, 相对路径, 完整路径) 的序列"""
    seen = set()
    for entry in entries:
        entry = os.path.abspath(entry)
        if not os.path.exists(entry):
            continue
        root_label = os.path.basename(entry.rstrip(os.sep)) or entry

        if os.path.isfile(entry):
            fname = os.path.basename(entry)
            if is_excluded(fname, fname, exclude_patterns):
                continue
            if entry not in seen:
                seen.add(entry)
                yield root_label, fname, entry

        elif os.path.isdir(entry):
            for cur_root, dirs, files in os.walk(entry):
                dirs[:] = [d for d in dirs if not is_excluded(d, d, exclude_patterns)]
                for f in files:
                    full = os.path.join(cur_root, f)
                    rel = os.path.relpath(full, entry)
                    if is_excluded(f, rel, exclude_patterns):
                        continue
                    if full in seen:
                        continue
                    seen.add(full)
                    yield root_label, rel, full


def detect_encoding(file_path):
    with open(file_path, "rb") as file:
        raw_data = file.read(100 * 1024)
        result = chardet.detect(raw_data)
        return result["encoding"]


def is_binary_file(file_path):
    try:
        with open(file_path, "rb") as file:
            chunk = file.read(1024)
            return b"\0" in chunk
    except Exception:
        return True


def process_entries(
    entries,
    exclude_patterns,
    max_file_size,
    truncate_size,
    include_content_txt=False,
    log_fn=print,
):
    """遍历所有条目，在内存中生成合集文本（可选在末尾追加 content.txt）"""
    combined_text = []
    file_count = 0

    for root_label, rel_path, full_path in iter_files_from_entries(entries, exclude_patterns):
        file_count += 1
        try:
            file_size = os.path.getsize(full_path)
        except OSError:
            continue

        if log_fn:
            log_fn(f"处理: {rel_path}")

        combined_text.append(f"\n{'=' * 50}\n")
        combined_text.append(f"File Path: {rel_path}\n")
        combined_text.append(f"Full Path: {full_path}\n")
        combined_text.append(f"File Size: {file_size} bytes\n")

        if is_binary_file(full_path):
            combined_text.append("[Binary file, content skipped]\n")
            continue

        try:
            encoding = detect_encoding(full_path) or "utf-8"
            with open(full_path, "r", encoding=encoding, errors="replace") as f:
                if file_size > max_file_size:
                    content = f.read(truncate_size)
                    combined_text.append(
                        f"File Content (Truncated to first {truncate_size // 1024}KB):\n"
                    )
                    combined_text.append(content)
                    combined_text.append(
                        f"\n\n[提示: 文件大小超过 {max_file_size // (1024 * 1024)}MB，"
                        f"仅截取前 {truncate_size // 1024}KB 内容]\n"
                    )
                else:
                    content = f.read()
                    combined_text.append("File Content:\n")
                    combined_text.append(content)
                    combined_text.append("\n")
        except Exception as e:
            combined_text.append(f"Error reading file: {str(e)}\n")

    # 如果启用了追加 content.txt，则在所有文件合并后追加
    if include_content_txt:
        extra_txt = read_content_txt()
        if extra_txt:
            combined_text.append(f"\n{'=' * 50}\n")
            combined_text.append(extra_txt)
            if not extra_txt.endswith("\n"):
                combined_text.append("\n")
            if log_fn:
                log_fn(f"已追加 {CONTENT_TXT_NAME} ({len(extra_txt)} 字符)")
        else:
            if log_fn:
                log_fn(f"提示: 勾选了追加 {CONTENT_TXT_NAME}，但该文件不存在或为空")

    return "".join(combined_text), file_count


def get_project_merged_memory(project_name: str, include_content_txt: bool | None = None, log_fn=None) -> tuple[str, int]:
    """
    直接根据项目名在内存中合成全文件合集（不生成任何磁盘 txt/pdf 中间文件）
    :param project_name: 项目名（对应 <project_name>.json）
    :param include_content_txt: 是否追加 content.txt；若为 None 则使用项目 json 中的设置
    :return: (合并后的完整文本, 文件数量)
    """
    cfg = load_project_config(project_name)
    use_content = cfg.get("append_content_txt", False) if include_content_txt is None else bool(include_content_txt)
    return process_entries(
        cfg.get("entries", []),
        cfg.get("excludes", DEFAULT_EXCLUDES),
        cfg.get("max_file_size", DEFAULT_MAX_FILE_SIZE),
        cfg.get("truncate_size", DEFAULT_TRUNCATE_SIZE),
        include_content_txt=use_content,
        log_fn=log_fn,
    )


def find_chinese_font():
    """在系统常见路径里找一个能用的中文字体，返回 (字体名, 文件路径) 或 (None, None)"""
    candidates = [
        ("SimSun", r"C:\Windows\Fonts\simsun.ttc"),
        ("MicrosoftYaHei", r"C:\Windows\Fonts\msyh.ttc"),
        ("MicrosoftYaHei", r"C:\Windows\Fonts\msyh.ttf"),
        ("DengXian", r"C:\Windows\Fonts\dengxian.ttf"),
    ]
    for font_name, path in candidates:
        if os.path.exists(path):
            return font_name, path
    return None, None


def text_to_pdf(input_file, output_file, font_size=10, line_spacing=1.2, log_fn=print):
    try:
        if os.path.getsize(input_file) == 0:
            log_fn("输入文件是空的，跳过 PDF 生成")
            return False

        c = canvas.Canvas(output_file, pagesize=A4)
        width, height = A4

        font_name = "Helvetica"
        cn_font_name, cn_font_path = find_chinese_font()
        if cn_font_path:
            try:
                pdfmetrics.registerFont(TTFont(cn_font_name, cn_font_path))
                font_name = cn_font_name
                log_fn(f"成功加载字体: {cn_font_name}")
            except Exception as e:
                log_fn(f"无法加载字体 {cn_font_name}: {e}")
        else:
            log_fn("未找到系统中文字体，中文可能显示异常")

        c.setFont(font_name, font_size)

        line_height = font_size * line_spacing
        margin = 40
        y = height - margin
        x = margin

        with open(input_file, "r", encoding="utf-8") as file:
            lines = file.readlines()
            if not lines:
                c.save()
                return False

            for line in lines:
                text = line.rstrip("\n")

                if y < margin:
                    c.showPage()
                    c.setFont(font_name, font_size)
                    y = height - margin

                if text:
                    max_width = width - 2 * margin
                    text_width = c.stringWidth(text, font_name, font_size)

                    if text_width <= max_width:
                        c.drawString(x, y, text)
                        y -= line_height
                    else:
                        current_line = ""
                        for ch in text:
                            test_line = current_line + ch
                            if c.stringWidth(test_line, font_name, font_size) <= max_width:
                                current_line = test_line
                            else:
                                if current_line:
                                    c.drawString(x, y, current_line)
                                    y -= line_height
                                    if y < margin:
                                        c.showPage()
                                        c.setFont(font_name, font_size)
                                        y = height - margin
                                current_line = ch
                        if current_line:
                            c.drawString(x, y, current_line)
                            y -= line_height
                else:
                    y -= line_height

        c.save()
        log_fn(f"PDF生成成功(已覆盖): {output_file}")
        return True

    except Exception as e:
        log_fn(f"转换过程中出现错误: {str(e)}")
        return False


def generate_reports(
    entries,
    excludes,
    max_file_size,
    truncate_size,
    include_content_txt=False,
    make_pdf=True,
    log_fn=print,
):
    """生成固定名称的 txt（及可选的 pdf）文件"""
    app_dir = get_app_dir()
    txt_path = os.path.join(app_dir, REPORT_TXT_NAME)
    pdf_path = os.path.join(app_dir, REPORT_PDF_NAME) if make_pdf else None

    log_fn(f"开始处理，共 {len(entries)} 个条目...")
    combined_text, file_count = process_entries(
        entries,
        excludes,
        max_file_size,
        truncate_size,
        include_content_txt=include_content_txt,
        log_fn=log_fn,
    )

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(combined_text)
    log_fn(f"文本合集已生成(已覆盖): {txt_path}（共 {file_count} 个文件）")

    if make_pdf:
        text_to_pdf(txt_path, pdf_path, log_fn=log_fn)
    else:
        log_fn("已跳过 PDF 生成")

    return combined_text, txt_path, pdf_path, file_count


# ==============================================================================
# ================= 合入 ai-execute-code.py 核心功能（已验证） =================
# ==============================================================================

# 循环高亮颜色池（深色背景高对比度，用于标注 >=2 个文件共享的绝对路径前缀）
PREFIX_COLORS = [
    "#4EC9B0",  # 青绿
    "#CE9178",  # 暖橙
    "#C586C0",  # 亮紫
    "#DCDCAA",  # 明黄
    "#569CD6",  # 湖蓝
    "#B5CEA8",  # 浅绿
]

OP_LABELS = {
    "write":  ("写入 WRITE ", "#2ea043"),
    "edit":   ("修改 EDIT  ", "#1f6feb"),
    "delete": ("删除 DELETE", "#da3633"),
    "copy":   ("复制 COPY  ", "#8957e5"),
    "cut":    ("剪切 CUT   ", "#d29922"),
    "move":   ("剪切 MOVE  ", "#d29922"),
    "rename": ("重命名 REN ", "#d29922"),
    "mkdir":  ("建目录 DIR ", "#6e7681"),
}

SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 使用拼接定义标签字面量，防止用本脚本修改本脚本自身时正则误匹配代码中的字符串
TAG_FILE_OPEN = "<<<" + "FILE"
TAG_SEARCH = "<<<" + "SEARCH>>>"
TAG_REPLACE = "<<<" + "REPLACE>>>"
TAG_END = "<<<" + "END>>>"
TAG_COMMIT = "<<<" + "COMMIT>>>"


def norm_path(p_str: str) -> str:
    """统一标准化为正斜杠绝对路径字符串，便于排序和前缀比对"""
    try:
        return pathlib.Path(p_str.strip()).resolve().as_posix()
    except Exception:
        return p_str.strip().replace("\\", "/")


def is_abs_path(p_str: str) -> bool:
    """严格检查是否为绝对路径（禁止 res:// 或相对路径）"""
    p_str = p_str.strip()
    if p_str.startswith("res://") or p_str.startswith("user://"):
        return False
    return os.path.isabs(p_str)


def read_file_smart(path_obj: pathlib.Path) -> tuple[str, str]:
    """
    智能读取文件文本及其原始换行符格式。
    返回: (统一为 \n 的文本内容, 原始换行符 "\r\n" 或 "\n")
    """
    raw_bytes = path_obj.read_bytes()
    eol = "\r\n" if b"\r\n" in raw_bytes else "\n"
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw_bytes.decode(enc)
            text = text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
            return text, eol
        except UnicodeDecodeError:
            continue
    text = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, eol


def preprocess_clipboard_text(text: str) -> str:
    """
    预处理大模型输出文本：
    兼容大模型在同一个 op="edit" 块内每组 <<<REPLACE>>> 后都写一个 <<<END>>> 的习惯。
    如果 <<<END>>> 后面（忽略空行）紧跟着的是 <<<SEARCH>>>，则将该多余的 <<<END>>> 自动移除合并。
    """
    merge_pattern = re.compile(
        r'(?m)^[ \t]*' + re.escape(TAG_END) + r'[ \t]*\n(?:[ \t]*\n)*(?=[ \t]*' + re.escape(TAG_SEARCH) + r'[ \t]*(?:\n|$))'
    )
    return merge_pattern.sub("", text)


def extract_non_patch_text(raw_text: str) -> tuple[str, bool]:
    """
    从大模型返回内容中剥离所有 <<<FILE...>>>...<<<END>>> 和 <<<COMMIT>>>...<<<END>>> 指令块，
    提取出纯自然语言回复文本（用于作为聊天记录展示与存储）。
    返回: (非指令纯文本回复, 是否检测到补丁标签)
    """
    norm_t = raw_text.replace("\r\n", "\n")
    preprocessed = preprocess_clipboard_text(norm_t)

    has_patch = bool(
        re.search(r'(?m)^[ \t]*' + re.escape(TAG_FILE_OPEN) + r'\s+', preprocessed)
        or re.search(r'(?m)^[ \t]*' + re.escape(TAG_SEARCH) + r'[ \t]*$', preprocessed)
    )

    # 移除完整闭合的 <<<FILE ...>>> ... <<<END>>>
    cleaned = re.sub(
        r'(?m)^[ \t]*' + re.escape(TAG_FILE_OPEN) + r'\s+[^>\n]+?>>>[ \t]*\n?.*?^[ \t]*' + re.escape(TAG_END) + r'[ \t]*$',
        '',
        preprocessed,
        flags=re.S,
    )
    # 移除 <<<COMMIT>>> ... <<<END>>>
    cleaned = re.sub(
        re.escape(TAG_COMMIT) + r'\s*.*?\s*' + re.escape(TAG_END),
        '',
        cleaned,
        flags=re.S,
    )
    # 清理包裹补丁块后残留的空代码块围栏 ```...```
    cleaned = re.sub(r'```[a-zA-Z0-9_-]*\s*```', '', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    if not cleaned and has_patch:
        cleaned = "（已生成代码修改补丁，请在补丁审核窗口中查看详情并执行）"

    return cleaned, has_patch


def parse_and_validate(clipboard_text: str):
    """
    解析文本内容并在内存虚拟文件系统(VFS)中预演校验。
    返回: (success: bool, actions: list, audit_rows: list, errors: list)
    """
    raw_text = clipboard_text.replace("\r\n", "\n")
    text = preprocess_clipboard_text(raw_text)
    errors = []
    actions = []

    file_pattern = re.compile(
        r'(?m)^[ \t]*' + re.escape(TAG_FILE_OPEN) + r'\s+([^>\n]+?)>>>[ \t]*\n?(.*?)^[ \t]*' + re.escape(TAG_END) + r'[ \t]*$',
        re.S
    )
    matches = list(file_pattern.finditer(text))

    if not matches:
        errors.append(
            f"未检测到任何有效的 {TAG_FILE_OPEN} ...>>> ... {TAG_END} 补丁块！\n"
            "请确认大模型的回复格式符合要求。"
        )
        return False, [], [], errors

    total_file_tags = len(re.findall(r'(?m)^[ \t]*' + re.escape(TAG_FILE_OPEN) + r'\s+[^>\n]+?>>>', text))
    total_search_tags = len(re.findall(r'(?m)^[ \t]*' + re.escape(TAG_SEARCH) + r'[ \t]*$', text))
    total_replace_tags = len(re.findall(r'(?m)^[ \t]*' + re.escape(TAG_REPLACE) + r'[ \t]*$', text))

    if total_file_tags != len(matches):
        errors.append(
            f"检测到 {total_file_tags} 个 {TAG_FILE_OPEN} 开头，但只有 {len(matches)} 个成功闭合了 {TAG_END}！\n"
            "可能有某个补丁块被截断或漏写了结束标记。"
        )

    if total_search_tags != total_replace_tags:
        errors.append(
            f"检测到 {TAG_SEARCH} 数量 ({total_search_tags}) 与 {TAG_REPLACE} 数量 ({total_replace_tags}) 不一致！\n"
            "请检查是否有未配对的替换块。"
        )

    vfs = {}
    vfs_eol = {}
    parsed_search_count = 0

    def vfs_exists(p_str: str) -> bool:
        if p_str in vfs:
            return vfs[p_str] is not None
        return pathlib.Path(p_str).exists()

    def vfs_read(p_str: str) -> str:
        if p_str in vfs:
            val = vfs[p_str]
            if val is None or val == "__DIR__":
                raise ValueError(f"路径不可作为文件读取: {p_str}")
            return val
        content, eol = read_file_smart(pathlib.Path(p_str))
        vfs_eol[p_str] = eol
        return content

    for idx, m in enumerate(matches, 1):
        attr_str, body = m.group(1), m.group(2)
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]+)"', attr_str))

        raw_path = attrs.get("path", "")
        raw_to = attrs.get("to", "")
        op = attrs.get("op", "").lower()

        if not raw_path or not op:
            errors.append(f"[块 #{idx}] 缺少 path 或 op 属性: {TAG_FILE_OPEN} {attr_str}>>>")
            continue

        if op not in OP_LABELS:
            errors.append(f"[块 #{idx}] 不支持的操作类型 op=\"{op}\" (支持: write/edit/delete/copy/cut/mkdir)")
            continue

        if not is_abs_path(raw_path):
            errors.append(f"[块 #{idx}] 路径必须是绝对路径，收到: \"{raw_path}\"")
            continue

        p = norm_path(raw_path)
        dst = norm_path(raw_to) if raw_to else ""

        if op in ("copy", "cut", "move", "rename"):
            if not raw_to:
                errors.append(f"[块 #{idx}] op=\"{op}\" 必须提供 to=\"目标绝对路径\" 属性 ({p})")
                continue
            if not is_abs_path(raw_to):
                errors.append(f"[块 #{idx}] 目标路径 to 必须是绝对路径，收到: \"{raw_to}\"")
                continue

        if op == "write":
            new_content = body.rstrip("\n") + "\n"
            old_content = ""
            if vfs_exists(p) and pathlib.Path(p).is_file():
                try:
                    old_content = vfs_read(p)
                except Exception:
                    pass
            vfs[p] = new_content
            actions.append({
                "op": "write", "path": p, "content": new_content,
                "eol": vfs_eol.get(p, "\n"),
                "preview": f"【全量写入文件】 {p}\n" + ("(覆盖已有文件)\n" if old_content else "(新建文件)\n") + "-" * 50 + "\n" + new_content
            })

        elif op == "edit":
            if not vfs_exists(p):
                errors.append(f"[块 #{idx}] 要修改的文件不存在: {p}")
                continue
            try:
                cur_text = vfs_read(p)
            except Exception as e:
                errors.append(f"[块 #{idx}] 读取文件失败 {p}: {e}")
                continue

            pair_pattern = re.compile(
                r'(?m)^[ \t]*' + re.escape(TAG_SEARCH) + r'[ \t]*\n(.*?)\n^[ \t]*' + re.escape(TAG_REPLACE) + r'[ \t]*(?:\n(.*?))?(?=\n^[ \t]*' + re.escape(TAG_SEARCH) + r'[ \t]*\n|\Z)',
                re.S
            )
            body_clean = body[:-1] if body.endswith("\n") else body
            pairs = [(m_p.group(1), m_p.group(2) if m_p.group(2) is not None else "") for m_p in pair_pattern.finditer(body_clean)]

            if not pairs:
                errors.append(f"[块 #{idx}] {p}: op=\"edit\" 中没有解析到 {TAG_SEARCH} / {TAG_REPLACE}")
                continue

            parsed_search_count += len(pairs)
            diff_lines = [f"【局部修改文件】 {p} (共 {len(pairs)} 处替换)\n" + "=" * 50]
            block_ok = True
            for s_idx, (s, r) in enumerate(pairs, 1):
                count = cur_text.count(s)
                if count == 0:
                    s_clean = "\n".join(line.rstrip() for line in s.split("\n"))
                    cur_clean = "\n".join(line.rstrip() for line in cur_text.split("\n"))
                    if cur_clean.count(s_clean) == 1:
                        lines_cur = cur_text.split("\n")
                        lines_cur_clean = cur_clean.split("\n")
                        lines_s_clean = s_clean.split("\n")
                        n_s = len(lines_s_clean)
                        for line_i in range(len(lines_cur_clean) - n_s + 1):
                            if lines_cur_clean[line_i:line_i + n_s] == lines_s_clean:
                                rep_list = [r] if r != "" else []
                                cur_text = "\n".join(lines_cur[:line_i] + rep_list + lines_cur[line_i + n_s:])
                                count = 1
                                break

                if count != 1:
                    errors.append(
                        f"[块 #{idx} 替换 #{s_idx}] {p}:\n"
                        f"SEARCH 匹配到了 {count} 次（必须恰好 1 次）。\n"
                        f"--- SEARCH 内容片段 ---\n{s[:250]}"
                    )
                    block_ok = False
                    break
                else:
                    if s in cur_text:
                        cur_text = cur_text.replace(s, r, 1)
                    diff_lines.append(f"\n--- 替换点 #{s_idx} ---")
                    for line in s.split("\n"):
                        diff_lines.append(f"- {line}")
                    if r:
                        for line in r.split("\n"):
                            diff_lines.append(f"+ {line}")

            if block_ok:
                vfs[p] = cur_text
                actions.append({
                    "op": "edit", "path": p, "content": cur_text,
                    "eol": vfs_eol.get(p, "\n"),
                    "preview": "\n".join(diff_lines)
                })

        elif op == "delete":
            if not vfs_exists(p):
                errors.append(f"[块 #{idx}] 要删除的路径不存在: {p}")
                continue
            vfs[p] = None
            actions.append({
                "op": "delete", "path": p,
                "preview": f"【删除路径】\n- {p}"
            })

        elif op == "copy":
            if not vfs_exists(p):
                errors.append(f"[块 #{idx}] 要复制的源路径不存在: {p}")
                continue
            try:
                if p in vfs and vfs[p] != "__DIR__":
                    vfs[dst] = vfs[p]
                elif pathlib.Path(p).is_file():
                    vfs[dst] = vfs_read(p)
                else:
                    vfs[dst] = "__DIR__"
            except Exception:
                vfs[dst] = "__DIR__"
            actions.append({
                "op": "copy", "path": p, "to": dst,
                "preview": f"【复制文件/目录】\n源路径: {p}\n目标路径: {dst}"
            })

        elif op in ("cut", "move", "rename"):
            if not vfs_exists(p):
                errors.append(f"[块 #{idx}] 要剪切/移动的源路径不存在: {p}")
                continue
            try:
                if p in vfs and vfs[p] != "__DIR__":
                    vfs[dst] = vfs[p]
                elif pathlib.Path(p).is_file():
                    vfs[dst] = vfs_read(p)
                else:
                    vfs[dst] = "__DIR__"
            except Exception:
                vfs[dst] = "__DIR__"
            vfs[p] = None
            actions.append({
                "op": op, "path": p, "to": dst,
                "preview": f"【剪切/移动文件或目录】\n源路径 (将被移除): {p}\n目标路径 (新位置): {dst}"
            })

        elif op == "mkdir":
            vfs[p] = "__DIR__"
            actions.append({
                "op": "mkdir", "path": p,
                "preview": f"【新建目录】\n+ {p}"
            })

    if not errors and parsed_search_count != total_search_tags:
        errors.append(
            f"检测到文本中共有 {total_search_tags} 处 {TAG_SEARCH}，但仅解析到 {parsed_search_count} 处！\n"
            f"有 {total_search_tags - parsed_search_count} 处修改块游离在 {TAG_FILE_OPEN} 块之外或格式有误，已阻止执行以防漏改。"
        )

    audit_rows = []
    for act in actions:
        op = act["op"]
        if op in ("copy", "cut", "move", "rename"):
            audit_rows.append({
                "path": act["path"], "op": op, "role": "(源)  ",
                "preview": act["preview"], "note": f"  ➜ 移至 {act['to']}"
            })
            audit_rows.append({
                "path": act["to"], "op": op, "role": "(目标)",
                "preview": act["preview"], "note": f"  ⬅ 来自 {act['path']}"
            })
        else:
            audit_rows.append({
                "path": act["path"], "op": op, "role": "      ",
                "preview": act["preview"], "note": ""
            })

    audit_rows.sort(key=lambda x: x["path"])
    return len(errors) == 0, actions, audit_rows, errors


def compute_prefix_coloring(audit_rows: list):
    """计算排序后相邻路径的公共前缀长度，并为 >=2 个文件共享的前缀分配循环颜色。"""
    n = len(audit_rows)
    if n == 0:
        return []

    def dir_part(p: str) -> str:
        idx = p.rfind("/")
        return p[:idx + 1] if idx != -1 else p

    def lcp_len(s1: str, s2: str) -> int:
        i = 0
        limit = min(len(s1), len(s2))
        while i < limit and s1[i] == s2[i]:
            i += 1
        return i

    dirs = [dir_part(row["path"]) for row in audit_rows]
    prefix_strs = []

    for i in range(n):
        best_len = 0
        if i > 0:
            best_len = max(best_len, lcp_len(dirs[i], dirs[i - 1]))
        if i < n - 1:
            best_len = max(best_len, lcp_len(dirs[i], dirs[i + 1]))
        prefix_strs.append(audit_rows[i]["path"][:best_len])

    prefix_to_color = {}
    color_idx = 0
    result = []
    for i, row in enumerate(audit_rows):
        p_str = prefix_strs[i]
        if len(p_str) > 3:
            if p_str not in prefix_to_color:
                prefix_to_color[p_str] = PREFIX_COLORS[color_idx % len(PREFIX_COLORS)]
                color_idx += 1
            color = prefix_to_color[p_str]
            plen = len(p_str)
        else:
            color = None
            plen = 0
        result.append((row, plen, color))

    return result


def execute_actions(actions: list):
    """真正将预演通过的操作写入磁盘"""
    for act in actions:
        op = act["op"]
        p = pathlib.Path(act["path"])

        if op in ("write", "edit"):
            p.parent.mkdir(parents=True, exist_ok=True)
            eol = act.get("eol", "\n")
            content = act["content"].replace("\r\n", "\n").replace("\r", "\n")
            if eol == "\r\n":
                content = content.replace("\n", "\r\n")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(content)

        elif op == "delete":
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)

        elif op == "copy":
            dst = pathlib.Path(act["to"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if p.is_dir():
                shutil.copytree(p, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(p, dst)

        elif op in ("cut", "move", "rename"):
            dst = pathlib.Path(act["to"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.is_file():
                dst.unlink()
            shutil.move(str(p), str(dst))

        elif op == "mkdir":
            p.mkdir(parents=True, exist_ok=True)


def generate_smart_commit_msg(clipboard_text: str, actions: list) -> tuple[str, str]:
    """智能生成 Git Commit 标题与正文"""
    op_cn = {
        "write": "写入", "edit": "修改", "delete": "删除",
        "copy": "复制", "cut": "移动", "move": "移动", "rename": "重命名", "mkdir": "建目录"
    }

    file_names = []
    body_lines = []
    for act in actions:
        op = act["op"]
        name = pathlib.Path(act["path"]).name
        if op in ("copy", "cut", "move", "rename"):
            to_name = pathlib.Path(act["to"]).name
            file_names.append(f"{name}->{to_name}")
            body_lines.append(f"- [{op_cn.get(op, op)}] {act['path']} -> {act['to']}")
        else:
            if name not in file_names:
                file_names.append(name)
            body_lines.append(f"- [{op_cn.get(op, op)}] {act['path']}")

    files_summary = ", ".join(file_names[:3])
    if len(file_names) > 3:
        files_summary += f" 等{len(file_names)}个文件"

    m_commit = re.search(re.escape(TAG_COMMIT) + r'\s*(.*?)\s*' + re.escape(TAG_END), clipboard_text, re.S)
    if m_commit:
        custom_msg = m_commit.group(1).strip().splitlines()
        title = custom_msg[0].strip() if custom_msg else "AI Patch"
        extra_body = "\n".join(custom_msg[1:]).strip()
        full_body = (extra_body + "\n\n" if extra_body else "") + "\n".join(body_lines)
        return title, full_body

    cleaned_text = preprocess_clipboard_text(clipboard_text.replace("\r\n", "\n"))
    outside = re.sub(
        r'(?m)^[ \t]*' + re.escape(TAG_FILE_OPEN) + r'\s+[^>\n]+?>>>.*?^[ \t]*' + re.escape(TAG_END) + r'[ \t]*$',
        '', cleaned_text, flags=re.S
    )
    outside = re.sub(r'```[\s\S]*?```', '', outside)

    ignore_prefixes = (
        "好的", "没问题", "明白", "理解", "当然", "以下是", "下面是",
        "我已经", "已为您", "请复制", "补丁如下", "代码如下", "---"
    )
    extracted_summary = ""
    for raw_line in outside.splitlines():
        clean = re.sub(r'^[\s#*>\-\d\.、：`*]+', '', raw_line).strip()
        clean = clean.replace("**", "").replace("`", "").strip(" ：:。！")
        if len(clean) < 4:
            continue
        for pfx in ignore_prefixes:
            if clean.startswith(pfx):
                clean = re.sub(r'^' + re.escape(pfx) + r'[，,。！!：:\s]*', '', clean)
        if len(clean) >= 4 and not clean.startswith(ignore_prefixes):
            extracted_summary = clean[:45] + ("..." if len(clean) > 45 else "")
            break

    if extracted_summary:
        title = f"AI: {extracted_summary} ({files_summary})"
    else:
        counts = {}
        for act in actions:
            label = op_cn.get(act["op"], act["op"])
            counts[label] = counts.get(label, 0) + 1
        stat_str = " / ".join(f"{k}{v}项" for k, v in counts.items())
        title = f"AI Patch: {stat_str} ({files_summary})"

    return title, "\n".join(body_lines)


def find_git_root(path_str: str) -> str | None:
    p = pathlib.Path(path_str)
    cur = p if p.is_dir() else p.parent
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    if not cur.exists():
        return None

    try:
        res = subprocess.run(
            ["git", "-C", str(cur), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=SUBPROCESS_FLAGS
        )
        if res.returncode == 0:
            return norm_path(res.stdout.strip())
    except FileNotFoundError:
        pass
    return None


def smart_git_commit(actions: list, commit_title: str, commit_body: str = "") -> tuple[bool, str]:
    repo_files: dict[str, set[str]] = {}
    for act in actions:
        paths = [act["path"]]
        if "to" in act and act["to"]:
            paths.append(act["to"])
        for p_str in paths:
            root = find_git_root(p_str)
            if root:
                repo_files.setdefault(root, set()).add(p_str)

    if not repo_files:
        return False, "目标路径不在任何 Git 仓库中，已跳过 Git Commit。"

    title = commit_title.strip() or "AI Patch"
    results = []

    for repo_root, file_set in repo_files.items():
        repo_name = pathlib.Path(repo_root).name
        rel_paths = []
        for f in file_set:
            try:
                rel = os.path.relpath(f, repo_root).replace("\\", "/")
                if not rel.startswith(".."):
                    rel_paths.append(rel)
            except ValueError:
                pass

        if not rel_paths:
            continue

        res_add = subprocess.run(
            ["git", "-C", repo_root, "add", "-A", "--"] + rel_paths,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=SUBPROCESS_FLAGS
        )
        if res_add.returncode != 0:
            return False, f"[{repo_name}] git add 失败:\n{res_add.stderr.strip()}"

        res_diff = subprocess.run(
            ["git", "-C", repo_root, "diff", "--cached", "--quiet"],
            creationflags=SUBPROCESS_FLAGS
        )
        if res_diff.returncode == 0:
            results.append(f"[{repo_name}] 无文件实质差异，无需提交")
            continue

        cmd_commit = ["git", "-C", repo_root, "commit", "-m", title]
        if commit_body.strip():
            cmd_commit.extend(["-m", commit_body.strip()])

        res_commit = subprocess.run(
            cmd_commit,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=SUBPROCESS_FLAGS
        )
        if res_commit.returncode != 0:
            err_msg = res_commit.stderr.strip() or res_commit.stdout.strip()
            return False, f"[{repo_name}] git commit 失败:\n{err_msg}"

        results.append(f"[{repo_name}] 已提交: {title}")

    return True, "\n".join(results)


def build_patch_audit_payload(raw_text: str) -> dict:
    """
    为网络接口（如 http_ui.py 网页端或其它 exe）构建结构化的补丁解析与审核数据包：
    同时返回剥离指令后的纯对话文本、补丁校验结果、彩色路径前缀数据、Diff 预览与智能 Commit 信息。
    """
    clean_reply, has_patch = extract_non_patch_text(raw_text)
    if not has_patch:
        return {
            "ok": True,
            "has_patch": False,
            "patch_valid": False,
            "clean_text": clean_reply,
            "actions_count": 0,
            "audit_rows": [],
            "errors": [],
            "all_previews": "",
            "commit_title": "",
            "commit_body": "",
        }

    valid, actions, audit_rows, errors = parse_and_validate(raw_text)
    colored = compute_prefix_coloring(audit_rows)

    serialized_rows = []
    for row, plen, pcolor in colored:
        op_key = row["op"]
        op_label, op_color = OP_LABELS.get(op_key, (op_key.upper(), "#555555"))
        serialized_rows.append({
            "path": row["path"],
            "op": op_key,
            "op_label": op_label,
            "op_color": op_color,
            "role": row["role"],
            "preview": row["preview"],
            "note": row["note"],
            "prefix_len": plen,
            "prefix_color": pcolor,
        })

    commit_title, commit_body = ("", "")
    if valid and actions:
        commit_title, commit_body = generate_smart_commit_msg(raw_text, actions)

    all_previews = (
        "\n\n".join(act["preview"] for act in actions)
        if valid
        else "【校验失败详情】\n\n" + "\n\n".join(f"❌ {e}" for e in errors)
    )

    return {
        "ok": True,
        "has_patch": True,
        "patch_valid": valid,
        "clean_text": clean_reply,
        "actions_count": len(actions),
        "audit_rows": serialized_rows,
        "errors": errors,
        "all_previews": all_previews,
        "commit_title": commit_title,
        "commit_body": commit_body,
    }


def show_audit_window(clipboard_text: str, parent=None):
    """本地 Tkinter 补丁审核窗口（保留原 ai-execute-code.py 完整体验）"""
    win = tk.Toplevel(parent) if parent else tk.Tk()
    win.title("AI 代码补丁审核器 (Clipboard Patch Auditor)")
    win.geometry("1020x660")
    win.configure(bg="#1e1e1e")
    win.attributes("-topmost", True)
    win.lift()
    win.focus_force()

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"1020x660+{(sw - 1020) // 2}+{(sh - 660) // 2}")

    ok, actions, audit_rows, errors = parse_and_validate(clipboard_text)

    banner_bg = "#1b4721" if ok else "#5a1d1d"
    banner_fg = "#7ee787" if ok else "#ffa198"
    banner_text = (
        f"✅ 补丁校验通过！共 {len(actions)} 项操作（涉及 {len(audit_rows)} 条路径）。请核对下方路径前缀颜色是否一致："
        if ok else
        f"❌ 补丁校验未通过（共 {len(errors)} 处错误）！未修改任何文件。"
    )
    lbl_banner = tk.Label(
        win, text=banner_text, bg=banner_bg, fg=banner_fg,
        font=("Microsoft YaHei UI", 11, "bold"), anchor="w", padx=15, pady=10
    )
    lbl_banner.pack(fill="x")

    paned = tk.PanedWindow(win, orient="vertical", bg="#333333", sashwidth=5)
    paned.pack(fill="both", expand=True, padx=10, pady=6)

    frame_top = tk.Frame(paned, bg="#1e1e1e")
    paned.add(frame_top, height=195)

    scroll_top = tk.Scrollbar(frame_top)
    scroll_top.pack(side="right", fill="y")

    txt_paths = tk.Text(
        frame_top, bg="#181818", fg="#d4d4d4", font=("Consolas", 11),
        yscrollcommand=scroll_top.set, wrap="none", cursor="hand2",
        padx=10, pady=8, spacing1=4, spacing3=4, relief="flat"
    )
    txt_paths.pack(fill="both", expand=True)
    scroll_top.config(command=txt_paths.yview)

    frame_bot = tk.Frame(paned, bg="#1e1e1e")
    paned.add(frame_bot, height=250)

    lbl_preview_title = tk.Label(
        frame_bot, text="📄 变更预览 ｜ 💡 提示：同色高亮代表共享目录前缀，颜色突变需警惕路径错误。点击上方路径可切换预览",
        bg="#252526", fg="#9cdcfe", font=("Microsoft YaHei UI", 9, "bold"), anchor="w", padx=10, pady=6
    )
    lbl_preview_title.pack(fill="x")

    scroll_bot = tk.Scrollbar(frame_bot)
    scroll_bot.pack(side="right", fill="y")

    txt_preview = tk.Text(
        frame_bot, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10),
        yscrollcommand=scroll_bot.set, wrap="none", padx=10, pady=8, relief="flat"
    )
    txt_preview.pack(fill="both", expand=True)
    scroll_bot.config(command=txt_preview.yview)

    txt_preview.tag_config("add", foreground="#7ee787", background="#122d1f")
    txt_preview.tag_config("del", foreground="#ffa198", background="#3c1618")
    txt_preview.tag_config("head", foreground="#569cd6", font=("Consolas", 10, "bold"))
    txt_preview.tag_config("err", foreground="#ff7b72")

    def render_preview(content: str, is_error=False):
        txt_preview.config(state="normal")
        txt_preview.delete("1.0", "end")
        if is_error:
            txt_preview.insert("end", content, "err")
        else:
            for line in content.split("\n"):
                if line.startswith("+ "):
                    txt_preview.insert("end", line + "\n", "add")
                elif line.startswith("- "):
                    txt_preview.insert("end", line + "\n", "del")
                elif line.startswith("【") or line.startswith("--- 替换点"):
                    txt_preview.insert("end", line + "\n", "head")
                else:
                    txt_preview.insert("end", line + "\n")
        txt_preview.config(state="disabled")

    colored_rows = compute_prefix_coloring(audit_rows)

    for op_key, (_, badge_color) in OP_LABELS.items():
        txt_paths.tag_config(f"op_{op_key}", foreground="#ffffff", background=badge_color)
    txt_paths.tag_config("role", foreground="#8b949e")
    txt_paths.tag_config("tail", foreground="#ffffff", font=("Consolas", 11, "bold"))
    txt_paths.tag_config("isolated", foreground="#ff7b72", underline=True)
    txt_paths.tag_config("note", foreground="#6e7681")

    for idx, (row, plen, pcolor) in enumerate(colored_rows):
        line_tag = f"row_{idx}"
        op_key = row["op"]
        op_text, _ = OP_LABELS.get(op_key, (op_key.upper(), "#555555"))

        txt_paths.insert("end", f" {op_text} ", (f"op_{op_key}", line_tag))
        txt_paths.insert("end", f" {row['role']} ", ("role", line_tag))

        full_p = row["path"]
        if plen > 0 and pcolor:
            ptag = f"prefix_{pcolor}"
            txt_paths.tag_config(ptag, foreground=pcolor, font=("Consolas", 11, "bold"))
            txt_paths.insert("end", full_p[:plen], (ptag, line_tag))
            txt_paths.insert("end", full_p[plen:], ("tail", line_tag))
        else:
            txt_paths.insert("end", full_p, ("isolated", line_tag))
            if len(colored_rows) > 1:
                txt_paths.insert("end", "  [⚠️无共同目录前缀!]", ("isolated", line_tag))

        if row["note"]:
            txt_paths.insert("end", row["note"], ("note", line_tag))
        txt_paths.insert("end", "\n")

        def on_row_click(event, preview_text=row["preview"]):
            render_preview(preview_text, is_error=False)

        txt_paths.tag_bind(line_tag, "<Button-1>", on_row_click)

    txt_paths.config(state="disabled")

    if ok:
        all_previews = "\n\n".join(act["preview"] for act in actions)
        render_preview(all_previews, is_error=False)
    else:
        err_report = "【校验失败详情】\n\n" + "\n\n".join(f"❌ {e}" for e in errors)
        render_preview(err_report, is_error=True)

    frame_btn = tk.Frame(win, bg="#252526", padx=15, pady=10)
    frame_btn.pack(fill="x", side="bottom")

    auto_commit_var = tk.BooleanVar(value=True)
    commit_msg_var = tk.StringVar(value="")
    commit_body_str = ""

    if ok:
        default_title, commit_body_str = generate_smart_commit_msg(clipboard_text, actions)
        commit_msg_var.set(default_title)

        chk_git = tk.Checkbutton(
            frame_btn, text="自动 Git Commit:", variable=auto_commit_var,
            bg="#252526", fg="#7ee787", selectcolor="#1e1e1e",
            activebackground="#252526", activeforeground="#7ee787",
            font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2"
        )
        chk_git.pack(side="left")

        entry_commit = tk.Entry(
            frame_btn, textvariable=commit_msg_var,
            bg="#181818", fg="#d4d4d4", insertbackground="white",
            font=("Microsoft YaHei UI", 9), relief="flat", highlightthickness=1,
            highlightbackground="#3c3c3c", highlightcolor="#569cd6"
        )
        entry_commit.pack(side="left", fill="x", expand=True, padx=(6, 15), ipady=4)

    def do_confirm(event=None):
        if not ok:
            return
        try:
            execute_actions(actions)
        except Exception as e:
            messagebox.showerror("执行写入异常", f"写盘过程中发生错误:\n{e}", parent=win)
            return

        if auto_commit_var.get():
            git_ok, git_info = smart_git_commit(actions, commit_msg_var.get(), commit_body_str)
            if not git_ok:
                messagebox.showwarning(
                    "文件已写入，但 Git Commit 未完成",
                    f"代码修改已成功写入磁盘！\n但自动 Git 提交遇到问题：\n\n{git_info}",
                    parent=win
                )
        win.destroy()

    def do_cancel(event=None):
        win.destroy()

    def do_copy_error():
        prompt_back = (
            "你的补丁在脚本自动校验时未通过，请只针对以下报错修正并重新输出失败的补丁块（路径必须是绝对路径）：\n\n"
            + "\n\n".join(errors)
        )
        win.clipboard_clear()
        win.clipboard_append(prompt_back)
        win.update()
        btn_copy_err.config(text="✅ 已复制报错！可直接粘贴给大模型", bg="#2ea043")

    if ok:
        btn_ok = tk.Button(
            frame_btn, text="✅ 确定执行并提交 (Enter)", command=do_confirm,
            bg="#2ea043", fg="white", activebackground="#238636", activeforeground="white",
            font=("Microsoft YaHei UI", 10, "bold"), padx=18, pady=5, relief="flat", cursor="hand2"
        )
        btn_ok.pack(side="right", padx=4)
        win.bind("<Return>", do_confirm)
    else:
        btn_copy_err = tk.Button(
            frame_btn, text="📋 一键复制报错信息 (发回给大模型)", command=do_copy_error,
            bg="#1f6feb", fg="white", activebackground="#388bfd", activeforeground="white",
            font=("Microsoft YaHei UI", 10, "bold"), padx=16, pady=5, relief="flat", cursor="hand2"
        )
        btn_copy_err.pack(side="right", padx=8)

    btn_cancel = tk.Button(
        frame_btn, text="❌ 取消 (Esc)", command=do_cancel,
        bg="#3c3c3c", fg="#cccccc", activebackground="#505050", activeforeground="white",
        font=("Microsoft YaHei UI", 10), padx=14, pady=5, relief="flat", cursor="hand2"
    )
    btn_cancel.pack(side="right", padx=6)
    win.bind("<Escape>", do_cancel)

    if not parent:
        win.mainloop()


# ==============================================================================
# ================= 核心局域网 HTTP API 服务 (Port 8124) =======================
# ==============================================================================

class CoreAPIHandler(BaseHTTPRequestHandler):
    """
    files_report_gui.py 内置的核心网络接口服务（供 http_ui.py 或其它本地 exe 跨进程/跨局域网调用）
    """
    app_ref = None  # 指向 GUI App 实例（如果有），便于同步刷新界面

    def log_message(self, format, *args):
        # 静默标准控制台日志，转发到 GUI 日志框
        if self.app_ref:
            self.app_ref._log(f"[API] {self.address_string()} - {format % args}")

    def _send_json(self, data: dict, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}

    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 1. 服务健康状态
        if route == "/api/status":
            projects = list_project_names()
            active = self.app_ref.current_project.get() if self.app_ref else projects[0]
            self._send_json({
                "ok": True,
                "service": "files_report_core",
                "core_port": CORE_API_PORT,
                "web_ui_port": WEB_UI_PORT,
                "lan_ip": get_lan_ip(),
                "active_project": active,
                "projects": projects,
            })

        # 2. 获取所有项目的名称（读取所有 .json 文件名）
        elif route == "/api/projects":
            projects = list_project_names()
            active = self.app_ref.current_project.get() if self.app_ref else projects[0]
            self._send_json({
                "ok": True,
                "projects": projects,
                "active": active,
            })

        # 3. 获取指定项目的详细配置 (.json 内容)
        elif route == "/api/project":
            name = qs.get("name", ["config"])[0]
            cfg = load_project_config(name)
            self._send_json({
                "ok": True,
                "name": sanitize_project_name(name),
                "config": cfg,
            })

        # 4. 获取指定项目名的全文件合集（直接在内存合成，不生成 txt 中间文件，可选追加 content.txt）
        elif route == "/api/merged_content":
            projects = list_project_names()
            default_proj = self.app_ref.current_project.get() if self.app_ref else projects[0]
            proj_name = qs.get("project", [default_proj])[0]
            inc_param = qs.get("include_content", [None])[0]
            if inc_param is not None:
                include_content = inc_param.lower() in ("1", "true", "yes")
            else:
                include_content = None

            try:
                merged_text, file_count = get_project_merged_memory(
                    proj_name,
                    include_content_txt=include_content,
                    log_fn=self.app_ref._log if self.app_ref else None,
                )
                self._send_json({
                    "ok": True,
                    "project": sanitize_project_name(proj_name),
                    "file_count": file_count,
                    "char_count": len(merged_text),
                    "content": merged_text,
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        # 5. 读取 content.txt
        elif route == "/api/content_txt":
            txt = read_content_txt()
            self._send_json({
                "ok": True,
                "exists": os.path.exists(get_content_txt_path()),
                "content": txt,
            })

        else:
            self._send_json({"ok": False, "error": f"Unknown GET endpoint: {route}"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            body = self._read_json_body()
        except Exception as e:
            self._send_json({"ok": False, "error": f"Invalid JSON body: {e}"}, status=400)
            return

        # 1. 新建/保存项目配置 .json
        if route == "/api/project/save":
            name = sanitize_project_name(body.get("name", "config"))
            old_cfg = load_project_config(name)
            entries = body.get("entries", old_cfg["entries"])
            excludes = body.get("excludes", old_cfg["excludes"])
            max_file_size = body.get("max_file_size", old_cfg["max_file_size"])
            truncate_size = body.get("truncate_size", old_cfg["truncate_size"])
            append_content_txt = body.get("append_content_txt", old_cfg["append_content_txt"])
            ok = save_project_config(name, entries, excludes, max_file_size, truncate_size, append_content_txt)
            if self.app_ref:
                self.app_ref.after(0, lambda: self.app_ref._on_external_project_change(name))
            self._send_json({"ok": ok, "name": name, "projects": list_project_names()})

        # 2. 重命名项目 .json
        elif route == "/api/project/rename":
            old_name = body.get("old_name", "")
            new_name = body.get("new_name", "")
            ok, msg_or_name = rename_project_config(old_name, new_name)
            if ok and self.app_ref:
                self.app_ref.after(0, lambda: self.app_ref._on_external_project_change(msg_or_name))
            self._send_json({"ok": ok, "name": msg_or_name, "projects": list_project_names()})

        # 3. 删除项目 .json
        elif route == "/api/project/delete":
            name = body.get("name", "")
            ok = delete_project_config(name)
            projects = list_project_names()
            if self.app_ref:
                self.app_ref.after(0, lambda: self.app_ref._on_external_project_change(projects[0]))
            self._send_json({"ok": ok, "projects": projects})

        # 4. 保存 content.txt
        elif route == "/api/content_txt":
            text = body.get("content", "")
            ok = write_content_txt(text)
            self._send_json({"ok": ok})

        # 5. 解析大模型返回文本：剥离指令提取纯对话回复 + VFS 预演校验补丁
        elif route == "/api/patch/parse":
            raw_text = body.get("text", "")
            payload = build_patch_audit_payload(raw_text)
            self._send_json(payload)

        # 6. 确认执行补丁写入 + 可选智能 Git Commit
        elif route == "/api/patch/execute":
            raw_text = body.get("text", "")
            auto_commit = bool(body.get("auto_commit", True))
            commit_title = body.get("commit_title", "")
            commit_body = body.get("commit_body", "")

            valid, actions, _, errors = parse_and_validate(raw_text)
            if not valid:
                self._send_json({
                    "ok": False,
                    "error": "补丁校验未通过，无法执行写入：\n" + "\n".join(errors),
                    "errors": errors,
                })
                return

            try:
                execute_actions(actions)
            except Exception as e:
                self._send_json({"ok": False, "error": f"写盘过程中发生异常: {e}"})
                return

            git_ok = True
            git_info = "未启用自动 Git Commit"
            if auto_commit:
                if not commit_title:
                    commit_title, commit_body = generate_smart_commit_msg(raw_text, actions)
                git_ok, git_info = smart_git_commit(actions, commit_title, commit_body)

            if self.app_ref:
                self.app_ref._log(f"[补丁已执行] 共写入 {len(actions)} 项操作 | Git: {git_info}")

            self._send_json({
                "ok": True,
                "executed_count": len(actions),
                "git_ok": git_ok,
                "git_info": git_info,
            })

        # 7. 在宿主机弹出本地 Tkinter 补丁审核窗口（方便其他本地 exe 调用）
        elif route == "/api/patch/show_gui_audit":
            raw_text = body.get("text", "")
            if self.app_ref:
                self.app_ref.after(0, lambda: show_audit_window(raw_text, parent=self.app_ref))
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "当前处于无 GUI 模式"})

        else:
            self._send_json({"ok": False, "error": f"Unknown POST endpoint: {route}"}, status=404)


def start_core_api_server(port=CORE_API_PORT, app_ref=None):
    """在后台守护线程启动核心局域网 HTTP API 服务"""
    CoreAPIHandler.app_ref = app_ref
    server = ThreadingHTTPServer(("0.0.0.0", port), CoreAPIHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ==============================================================================
# ================= 命令行无界面模式 (CLI) =====================================
# ==============================================================================

def copy_to_clipboard_headless(text):
    """不显示主界面的情况下将文本写入系统剪贴板"""
    root = tk.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    root.after(150, root.destroy)
    root.mainloop()


def run_cli(args):
    """处理命令行参数启动，执行完毕后直接退出，不启动主 GUI"""
    # 解析命令行参数
    project_name = None
    with_content = None
    action = None

    i = 0
    while i < len(args):
        a = args[i]
        al = a.lower()
        if al in ("--list-projects", "-l"):
            action = "list"
        elif al in ("--project", "-p") and i + 1 < len(args):
            project_name = args[i + 1]
            i += 1
        elif al.startswith("--project="):
            project_name = a.split("=", 1)[1]
        elif al in ("--with-content", "-w"):
            with_content = True
        elif al == "--no-content":
            with_content = False
        elif al in ("--stdout", "-s"):
            action = "stdout"
        elif al in ("--mem-copy", "-m"):
            action = "mem_copy"
        elif al in ("--txt-copy", "-t"):
            action = "txt_copy"
        elif al in ("--copy", "-c"):
            action = "copy"
        elif al in ("--gen", "-g"):
            action = "gen"
        elif al in ("--audit-clip", "-a"):
            action = "audit_clip"
        i += 1

    if action == "list":
        for name in list_project_names():
            print(name)
        return

    if action == "audit_clip":
        temp_root = tk.Tk()
        temp_root.withdraw()
        try:
            clip_data = temp_root.clipboard_get()
        except Exception:
            clip_data = ""
        temp_root.destroy()
        show_audit_window(clip_data)
        return

    if action not in ("stdout", "mem_copy", "txt_copy", "copy", "gen"):
        print("支持的参数：")
        print("  --list-projects  或 -l        : 获取所有项目的名称（读取所有 .json 文件名）")
        print("  --project <名>   或 -p <名>   : 指定项目名称（默认使用 config）")
        print("  --with-content   或 -w        : 在合并文件末尾追加 content.txt 内容")
        print("  --stdout         或 -s        : 获取指定项目的全文件合集直接输出（不生成 txt 中间文件）")
        print("  --mem-copy       或 -m        : 获取指定项目的全文件合集并复制到剪贴板（不生成 txt 中间文件）")
        print("  --gen            或 -g        : 生成 txt + pdf 合集文件（直接覆盖）")
        print("  --copy           或 -c        : 生成 txt + pdf 合集文件并复制到剪贴板")
        print("  --txt-copy       或 -t        : 仅生成 txt 合集文件并复制到剪贴板")
        print("  --audit-clip     或 -a        : 弹出剪贴板 AI 补丁审核器窗口")
        return

    projects = list_project_names()
    target_proj = sanitize_project_name(project_name) if project_name else projects[0]
    cfg = load_project_config(target_proj)
    entries = cfg.get("entries", [])
    excludes = cfg.get("excludes", DEFAULT_EXCLUDES)
    max_file_size = cfg.get("max_file_size", DEFAULT_MAX_FILE_SIZE)
    truncate_size = cfg.get("truncate_size", DEFAULT_TRUNCATE_SIZE)
    use_content = cfg.get("append_content_txt", False) if with_content is None else with_content

    if not entries:
        print(f"项目 [{target_proj}] 未找到需要处理的条目，请先启动程序添加文件/文件夹。")
        return

    # 不生成任何磁盘中间文件的模式
    if action in ("stdout", "mem_copy"):
        combined_text, file_count = process_entries(
            entries,
            excludes,
            max_file_size,
            truncate_size,
            include_content_txt=use_content,
            log_fn=None if action == "stdout" else print,
        )
        if action == "stdout":
            sys.stdout.buffer.write(combined_text.encode("utf-8"))
        else:
            copy_to_clipboard_headless(combined_text)
            print(f"[{target_proj}] 已在内存中合成 {file_count} 个文件并复制到剪贴板（未生成中间文件）")
        return

    # 传统生成磁盘文件模式
    make_pdf = action in ("gen", "copy")
    copy_clip = action in ("copy", "txt_copy")
    combined_text, _, _, _ = generate_reports(
        entries,
        excludes,
        max_file_size,
        truncate_size,
        include_content_txt=use_content,
        make_pdf=make_pdf,
        log_fn=print,
    )
    if copy_clip:
        copy_to_clipboard_headless(combined_text)
        print("内容已复制到剪贴板")


# ==============================================================================
# ================= 总入口图形界面 (Tkinter GUI) ===============================
# ==============================================================================

BaseTk = TkinterDnD.Tk if HAS_DND else tk.Tk


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("文件内容合集生成工具 & 远程 AI 编程总控台")
        self.geometry("880x700")
        self.minsize(760, 580)

        self.log_queue = queue.Queue()
        self.http_ui_proc = None
        self.core_server = None

        projects = list_project_names()
        self.current_project = tk.StringVar(value=projects[0])

        cfg = load_project_config(self.current_project.get())
        self.entries = cfg["entries"]
        self.excludes = cfg["excludes"]
        self.max_file_size = tk.IntVar(value=cfg["max_file_size"])
        self.truncate_size = tk.IntVar(value=cfg["truncate_size"])
        self.append_content_txt = tk.BooleanVar(value=cfg.get("append_content_txt", False))

        self._build_ui()
        self._refresh_project_combo()
        self._refresh_entries_list()
        self._refresh_excludes_list()
        self._poll_log_queue()

        # 启动内置 Core HTTP API 服务 (8124)
        self._start_core_server()
        # 自动尝试启动独立 http_ui.py 网页服务器 (8123)
        self.after(300, self.start_http_ui_server)
        # 定时检测双服务器状态
        self._poll_server_status()

        if not HAS_DND:
            self._log(
                "提示：未安装 tkinterdnd2，拖拽添加功能不可用，"
                "可用下方按钮添加文件/文件夹。"
            )

    # ---------- 界面搭建 ----------
    def _build_ui(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(2, weight=1)

        # --- 0. 顶部：多项目管理栏 + HTTP 服务器状态监控栏 ---
        top_bar = ttk.Frame(main)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        top_bar.columnconfigure(1, weight=1)

        # 左侧：多项目选择与管理
        proj_frame = ttk.LabelFrame(top_bar, text="当前项目 (.json 独立保存文件列表与右侧排除设置)", padding=6)
        proj_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(proj_frame, text="选择项目:").pack(side=tk.LEFT, padx=(2, 4))
        self.proj_combo = ttk.Combobox(
            proj_frame, textvariable=self.current_project, state="readonly", width=18
        )
        self.proj_combo.pack(side=tk.LEFT, padx=2)
        self.proj_combo.bind("<<ComboboxSelected>>", self.on_project_selected)

        ttk.Button(proj_frame, text="新建项目", command=self.new_project).pack(side=tk.LEFT, padx=2)
        ttk.Button(proj_frame, text="重命名", command=self.rename_project).pack(side=tk.LEFT, padx=2)
        ttk.Button(proj_frame, text="删除项目", command=self.delete_project).pack(side=tk.LEFT, padx=2)

        # 右侧：双网络服务状态监控栏
        net_frame = ttk.LabelFrame(top_bar, text="局域网 HTTP 服务状态 (总启动入口)", padding=6)
        net_frame.grid(row=0, column=1, sticky="nsew")

        self.lbl_core_status = tk.Label(
            net_frame, text="● 核心API(8124): 启动中...", fg="#d29922", font=("Microsoft YaHei UI", 9, "bold")
        )
        self.lbl_core_status.pack(side=tk.LEFT, padx=(4, 10))

        self.lbl_webui_status = tk.Label(
            net_frame, text="● 网页端(8123): 检测中...", fg="#d29922", font=("Microsoft YaHei UI", 9, "bold")
        )
        self.lbl_webui_status.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_toggle_webui = ttk.Button(net_frame, text="启动网页服务", command=self.toggle_http_ui_server)
        self.btn_toggle_webui.pack(side=tk.LEFT, padx=2)

        ttk.Button(net_frame, text="🌐 打开网页端(8123)", command=self.open_web_ui).pack(side=tk.LEFT, padx=2)

        # --- 1. 左栏：条目列表 ---
        entries_frame = ttk.LabelFrame(main, text="要处理的文件 / 文件夹（可拖拽添加）")
        entries_frame.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(0, 5))
        entries_frame.rowconfigure(1, weight=1)
        entries_frame.columnconfigure(0, weight=1)

        btn_row = ttk.Frame(entries_frame)
        btn_row.grid(row=0, column=0, sticky="ew", pady=(4, 4))
        ttk.Button(btn_row, text="添加文件", command=self.add_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="添加文件夹", command=self.add_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="删除选中", command=self.remove_selected_entry).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="清空", command=self.clear_entries).pack(side=tk.LEFT, padx=2)

        self.entries_list = tk.Listbox(entries_frame, selectmode=tk.EXTENDED)
        self.entries_list.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        scroll1 = ttk.Scrollbar(entries_frame, orient="vertical", command=self.entries_list.yview)
        scroll1.grid(row=1, column=1, sticky="ns")
        self.entries_list.configure(yscrollcommand=scroll1.set)

        if HAS_DND:
            self.entries_list.drop_target_register(DND_FILES)
            self.entries_list.dnd_bind("<<Drop>>", self.on_drop)

        # --- 2. 右上：排除规则列表 ---
        excl_frame = ttk.LabelFrame(main, text="限制条目 / 排除规则（随当前项目 .json 保存，支持 *.log 等通配符）")
        excl_frame.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        excl_frame.columnconfigure(0, weight=1)
        excl_frame.rowconfigure(1, weight=1)

        excl_btn_row = ttk.Frame(excl_frame)
        excl_btn_row.grid(row=0, column=0, sticky="ew", pady=(4, 4))
        self.exclude_entry = ttk.Entry(excl_btn_row)
        self.exclude_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 4))
        self.exclude_entry.bind("<Return>", lambda e: self.add_exclude())
        ttk.Button(excl_btn_row, text="添加", command=self.add_exclude).pack(side=tk.LEFT, padx=2)
        ttk.Button(excl_btn_row, text="删除选中", command=self.remove_selected_exclude).pack(side=tk.LEFT, padx=2)

        self.excl_list = tk.Listbox(excl_frame, selectmode=tk.EXTENDED)
        self.excl_list.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        scroll2 = ttk.Scrollbar(excl_frame, orient="vertical", command=self.excl_list.yview)
        scroll2.grid(row=1, column=1, sticky="ns")
        self.excl_list.configure(yscrollcommand=scroll2.set)

        # --- 3. 右下：限制参数与 content.txt 追加设置 ---
        opts_frame = ttk.LabelFrame(main, text="单文件大小限制 & 尾部追加设置")
        opts_frame.grid(row=2, column=1, sticky="new", padx=(5, 0), pady=(8, 0))
        ttk.Label(opts_frame, text="超过多少字节视为大文件:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(opts_frame, textvariable=self.max_file_size, width=12).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(opts_frame, text="大文件截取前多少字节:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(opts_frame, textvariable=self.truncate_size, width=12).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(opts_frame, text="(单位：字节，1MB=1048576)").grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

        content_row = ttk.Frame(opts_frame)
        content_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=(6, 4))
        ttk.Checkbutton(
            content_row,
            text="合并所有文件后末尾追加 content.txt 内容",
            variable=self.append_content_txt,
            command=self._save,
        ).pack(side=tk.LEFT)
        ttk.Button(content_row, text="编辑 content.txt", command=self.edit_content_txt_dialog).pack(side=tk.LEFT, padx=8)

        # --- 4. 底部：生成按钮 + AI 补丁审核按钮 + 日志 ---
        bottom = ttk.Frame(main)
        bottom.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        bottom.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        gen_btn_row = ttk.Frame(bottom)
        gen_btn_row.pack(anchor="w", pady=(0, 6), fill=tk.X)
        self.gen_btn = ttk.Button(gen_btn_row, text="生成合集(txt+pdf)", command=self.start_generate)
        self.gen_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.gen_copy_btn = ttk.Button(
            gen_btn_row, text="生成合集并复制到剪贴板", command=self.start_generate_and_copy
        )
        self.gen_copy_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.mem_copy_btn = ttk.Button(
            gen_btn_row, text="仅内存合成并复制(不生成文件)", command=self.start_mem_copy
        )
        self.mem_copy_btn.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Button(
            gen_btn_row, text="🔍 审核剪贴板AI补丁", command=self.audit_clipboard
        ).pack(side=tk.RIGHT, padx=(6, 0))

        self.log_box = scrolledtext.ScrolledText(bottom, height=12, state="disabled")
        self.log_box.pack(fill=tk.BOTH, expand=True)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 多项目管理操作 ----------
    def _refresh_project_combo(self):
        names = list_project_names()
        self.proj_combo["values"] = names
        if self.current_project.get() not in names:
            self.current_project.set(names[0])

    def on_project_selected(self, event=None):
        proj = self.current_project.get()
        self._load_project_into_ui(proj)
        self._log(f"已切换到项目: {proj}.json")

    def _load_project_into_ui(self, proj_name: str):
        cfg = load_project_config(proj_name)
        self.entries = cfg.get("entries", [])
        self.excludes = cfg.get("excludes", DEFAULT_EXCLUDES.copy())
        self.max_file_size.set(cfg.get("max_file_size", DEFAULT_MAX_FILE_SIZE))
        self.truncate_size.set(cfg.get("truncate_size", DEFAULT_TRUNCATE_SIZE))
        self.append_content_txt.set(cfg.get("append_content_txt", False))
        self._refresh_entries_list()
        self._refresh_excludes_list()

    def _on_external_project_change(self, target_proj: str):
        """当通过 HTTP API 修改了项目配置时，自动同步刷新 GUI"""
        self._refresh_project_combo()
        if target_proj in self.proj_combo["values"]:
            self.current_project.set(target_proj)
            self._load_project_into_ui(target_proj)

    def new_project(self):
        name = simpledialog.askstring("新建项目", "请输入新项目名称（将保存为 同名.json）：", parent=self)
        if not name:
            return
        safe_name = sanitize_project_name(name)
        if safe_name in list_project_names():
            messagebox.showwarning("提示", f"项目 [{safe_name}.json] 已存在！", parent=self)
            return
        # 默认继承当前的排除规则和大小限制，条目清空便于添加新项目目录
        save_project_config(
            safe_name,
            [],
            self.excludes.copy(),
            self.max_file_size.get(),
            self.truncate_size.get(),
            self.append_content_txt.get(),
        )
        self._refresh_project_combo()
        self.current_project.set(safe_name)
        self._load_project_into_ui(safe_name)
        self._log(f"已新建并切换到项目: {safe_name}.json")

    def rename_project(self):
        old_name = self.current_project.get()
        new_name = simpledialog.askstring(
            "重命名项目", f"将项目 [{old_name}.json] 重命名为：", initialvalue=old_name, parent=self
        )
        if not new_name or new_name.strip() == old_name:
            return
        ok, res = rename_project_config(old_name, new_name)
        if not ok:
            messagebox.showerror("重命名失败", res, parent=self)
            return
        self._refresh_project_combo()
        self.current_project.set(res)
        self._log(f"项目已重命名: {old_name}.json -> {res}.json")

    def delete_project(self):
        cur = self.current_project.get()
        names = list_project_names()
        if len(names) <= 1:
            messagebox.showwarning("提示", "至少需要保留一个项目配置文件！", parent=self)
            return
        if not messagebox.askyesno("确认删除", f"确定要删除项目配置文件 [{cur}.json] 吗？", parent=self):
            return
        delete_project_config(cur)
        self._refresh_project_combo()
        new_cur = self.current_project.get()
        self._load_project_into_ui(new_cur)
        self._log(f"已删除项目: {cur}.json，当前项目: {new_cur}.json")

    def edit_content_txt_dialog(self):
        """弹窗直接编辑同目录下的 content.txt"""
        dlg = tk.Toplevel(self)
        dlg.title(f"编辑 {CONTENT_TXT_NAME}（勾选追加后会自动附在合并文件末尾）")
        dlg.geometry("680x460")
        dlg.transient(self)

        txt_widget = scrolledtext.ScrolledText(dlg, font=("Consolas", 10))
        txt_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        txt_widget.insert("1.0", read_content_txt())

        btn_bar = ttk.Frame(dlg, padding=(10, 0, 10, 10))
        btn_bar.pack(fill=tk.X)

        def save_and_close():
            content = txt_widget.get("1.0", tk.END).rstrip("\n")
            if write_content_txt(content):
                self._log(f"已保存 {CONTENT_TXT_NAME} ({len(content)} 字符)")
                dlg.destroy()
            else:
                messagebox.showerror("错误", f"保存 {CONTENT_TXT_NAME} 失败", parent=dlg)

        ttk.Button(btn_bar, text="💾 保存 content.txt", command=save_and_close).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_bar, text="取消", command=dlg.destroy).pack(side=tk.RIGHT, padx=4)

    # ---------- 网络服务与 http_ui.py 进程控制 ----------
    def _start_core_server(self):
        try:
            self.core_server = start_core_api_server(port=CORE_API_PORT, app_ref=self)
            lan_ip = get_lan_ip()
            self._log(f"✅ 核心 API 服务已启动: http://{lan_ip}:{CORE_API_PORT} (供 http_ui.py 及外部 exe 调用)")
        except Exception as e:
            self._log(f"❌ 核心 API 服务端口 {CORE_API_PORT} 启动失败: {e}")

    def _is_port_open(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except Exception:
            return False

    def start_http_ui_server(self):
        if self._is_port_open(WEB_UI_PORT):
            self._log(f"🌐 检测到网页端服务已在端口 {WEB_UI_PORT} 运行中")
            return
        http_ui_script = os.path.join(get_app_dir(), "http_ui.py")
        if not os.path.exists(http_ui_script):
            self._log(f"提示: 未找到 {http_ui_script}，请先创建 http_ui.py")
            return
        try:
            self.http_ui_proc = subprocess.Popen(
                [sys.executable, http_ui_script],
                cwd=get_app_dir(),
                creationflags=SUBPROCESS_FLAGS,
            )
            lan_ip = get_lan_ip()
            self._log(f"🚀 已启动独立网页服务 http_ui.py -> http://{lan_ip}:{WEB_UI_PORT}")
        except Exception as e:
            self._log(f"启动 http_ui.py 失败: {e}")

    def stop_http_ui_server(self):
        if self.http_ui_proc and self.http_ui_proc.poll() is None:
            try:
                self.http_ui_proc.terminate()
                self.http_ui_proc.wait(timeout=2)
            except Exception:
                pass
            self.http_ui_proc = None
            self._log("已停止由本程序启动的 http_ui.py 网页服务")
        else:
            self._log("提示: http_ui.py 并非由当前窗口启动或已停止")

    def toggle_http_ui_server(self):
        if self._is_port_open(WEB_UI_PORT):
            self.stop_http_ui_server()
        else:
            self.start_http_ui_server()

    def open_web_ui(self):
        if not self._is_port_open(WEB_UI_PORT):
            self.start_http_ui_server()
            time.sleep(0.4)
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{WEB_UI_PORT}")

    def _poll_server_status(self):
        lan_ip = get_lan_ip()
        core_ok = self._is_port_open(CORE_API_PORT)
        web_ok = self._is_port_open(WEB_UI_PORT)

        if core_ok:
            self.lbl_core_status.config(text=f"● 核心API({CORE_API_PORT}): 运行中", fg="#2ea043")
        else:
            self.lbl_core_status.config(text=f"● 核心API({CORE_API_PORT}): 未运行", fg="#da3633")

        if web_ok:
            self.lbl_webui_status.config(text=f"● 网页端({lan_ip}:{WEB_UI_PORT}): 运行中", fg="#2ea043")
            self.btn_toggle_webui.config(text="停止网页服务")
        else:
            self.lbl_webui_status.config(text=f"● 网页端({WEB_UI_PORT}): 未运行", fg="#da3633")
            self.btn_toggle_webui.config(text="启动网页服务")

        self.after(2000, self._poll_server_status)

    # ---------- 条目操作 ----------
    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择一个或多个文件")
        for p in paths:
            self._add_entry(p)
        self._save()

    def add_folder(self):
        path = filedialog.askdirectory(title="选择一个文件夹（可重复点击本按钮添加多个）")
        if path:
            self._add_entry(path)
            self._save()

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        for p in paths:
            self._add_entry(p)
        self._save()

    def _add_entry(self, path):
        path = os.path.abspath(path)
        if path not in self.entries:
            self.entries.append(path)
            self._refresh_entries_list()

    def remove_selected_entry(self):
        sel = list(self.entries_list.curselection())
        for idx in reversed(sel):
            del self.entries[idx]
        self._refresh_entries_list()
        self._save()

    def clear_entries(self):
        if self.entries and messagebox.askyesno("确认", "清空当前项目所有已添加的文件/文件夹？"):
            self.entries.clear()
            self._refresh_entries_list()
            self._save()

    def _refresh_entries_list(self):
        self.entries_list.delete(0, tk.END)
        for e in self.entries:
            tag = "📁" if os.path.isdir(e) else "📄"
            self.entries_list.insert(tk.END, f"{tag} {e}")

    # ---------- 排除规则操作 ----------
    def add_exclude(self):
        val = self.exclude_entry.get().strip()
        if val and val not in self.excludes:
            self.excludes.append(val)
            self.exclude_entry.delete(0, tk.END)
            self._refresh_excludes_list()
            self._save()

    def remove_selected_exclude(self):
        sel = list(self.excl_list.curselection())
        for idx in reversed(sel):
            del self.excludes[idx]
        self._refresh_excludes_list()
        self._save()

    def _refresh_excludes_list(self):
        self.excl_list.delete(0, tk.END)
        for p in self.excludes:
            self.excl_list.insert(tk.END, p)

    # ---------- 保存配置 ----------
    def _save(self):
        try:
            max_sz = int(self.max_file_size.get())
        except Exception:
            max_sz = DEFAULT_MAX_FILE_SIZE
        try:
            trunc_sz = int(self.truncate_size.get())
        except Exception:
            trunc_sz = DEFAULT_TRUNCATE_SIZE

        save_project_config(
            self.current_project.get(),
            self.entries,
            self.excludes,
            max_sz,
            trunc_sz,
            self.append_content_txt.get(),
        )

    def on_close(self):
        self._save()
        if self.http_ui_proc and self.http_ui_proc.poll() is None:
            try:
                self.http_ui_proc.terminate()
            except Exception:
                pass
        self.destroy()

    # ---------- 日志 ----------
    def _log(self, msg):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert(tk.END, msg + "\n")
                self.log_box.see(tk.END)
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    # ---------- 生成合集与补丁审核 ----------
    def audit_clipboard(self):
        try:
            clip_data = self.clipboard_get()
        except Exception:
            clip_data = ""
        show_audit_window(clip_data, parent=self)

    def start_generate(self):
        self._start_generate(copy_to_clipboard=False, mem_only=False)

    def start_generate_and_copy(self):
        self._start_generate(copy_to_clipboard=True, mem_only=False)

    def start_mem_copy(self):
        self._start_generate(copy_to_clipboard=True, mem_only=True)

    def _start_generate(self, copy_to_clipboard, mem_only=False):
        if not self.entries:
            messagebox.showwarning("提示", "请先添加至少一个文件或文件夹")
            return
        self._save()
        self.gen_btn.configure(state="disabled")
        self.gen_copy_btn.configure(state="disabled")
        self.mem_copy_btn.configure(state="disabled")
        threading.Thread(
            target=self._generate_worker, args=(copy_to_clipboard, mem_only), daemon=True
        ).start()

    def _copy_to_clipboard(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self._log("已复制到剪贴板")

    def _generate_worker(self, copy_to_clipboard, mem_only):
        try:
            if mem_only:
                combined_text, file_count = process_entries(
                    self.entries,
                    self.excludes,
                    self.max_file_size.get(),
                    self.truncate_size.get(),
                    include_content_txt=self.append_content_txt.get(),
                    log_fn=self._log,
                )
                self.after(0, lambda: self._copy_to_clipboard(combined_text))
                self._log(f"内存合成完成（共 {file_count} 个文件，{len(combined_text)} 字符），未生成磁盘中间文件。")
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "完成", f"已在内存中合成 {file_count} 个文件内容并复制到剪贴板！\n（未生成 txt/pdf 磁盘文件）"
                    ),
                )
            else:
                combined_text, txt_path, pdf_path, _ = generate_reports(
                    self.entries,
                    self.excludes,
                    self.max_file_size.get(),
                    self.truncate_size.get(),
                    include_content_txt=self.append_content_txt.get(),
                    make_pdf=True,
                    log_fn=self._log,
                )
                if copy_to_clipboard:
                    self.after(0, lambda: self._copy_to_clipboard(combined_text))

                self._log("全部完成！")
                msg = f"合集已生成（已覆盖旧文件）：\n{txt_path}"
                if pdf_path:
                    msg += f"\n{pdf_path}"
                if copy_to_clipboard:
                    msg += "\n\n内容已复制到剪贴板"
                self.after(0, lambda: messagebox.showinfo("完成", msg))
        except Exception as e:
            self._log(f"发生错误: {e}")
            self.after(0, lambda: messagebox.showerror("错误", str(e)))
        finally:
            def reset_buttons():
                self.gen_btn.configure(state="normal", text="生成合集(txt+pdf)")
                self.gen_copy_btn.configure(state="normal", text="生成合集并复制到剪贴板")
                self.mem_copy_btn.configure(state="normal", text="仅内存合成并复制(不生成文件)")
            self.after(0, reset_buttons)


def main():
    # 防止 PyInstaller -w (无控制台窗口) 模式下 print() 遇到 sys.stdout 为 None 报错
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    # 关键：当打包为 collectcode.exe 后，self.start_http_ui_server() 会调用:
    #   collectcode.exe http_ui.py
    # 这里直接用 exe 内置的 Python 环境执行外部的 http_ui.py，且支持动态导入外部的 llm_client.py！
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".py"):
        script_path = os.path.abspath(sys.argv[1])
        app_dir = get_app_dir()
        if app_dir not in sys.path:
            sys.path.insert(0, app_dir)
        sys.argv = sys.argv[1:]
        runpy.run_path(script_path, run_name="__main__")
        return

    # 普通命令行参数模式 (--list-projects, --gen, --copy 等)
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
        return

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()