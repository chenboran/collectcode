# --- START OF FILE launcher.py ---
# -*- coding: utf-8 -*-
"""
通用 EXE 启动器底座 (打包为 collectcode.exe)
功能：
  1. 默认启动同目录下的 files_report_gui.py（并透传所有命令行参数，如 -g, -c, -l 等）
  2. 如果第一个参数是 xxx.py（例如 collectcode.exe http_ui.py），则直接运行该 py 脚本
  3. 预加载全部依赖库，实现所有业务 .py 文件 100% 外部化、随时用记事本修改免重新编译
"""

import os
import sys
import runpy
import traceback

# ==================== 预导入依赖库（确保 PyInstaller 完整打包） ====================
import re
import io
import csv
import copy
import math
import json
import time
import uuid
import hmac
import base64
import shutil
import socket
import ctypes
import random
import hashlib
import fnmatch
import pathlib
import sqlite3
import zipfile
import logging
import tempfile
import argparse
import datetime
import threading
import subprocess
import webbrowser
import queue
import urllib.request
import urllib.parse
import urllib.error
import http.server
import http.client

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk, simpledialog, colorchooser, font

import chardet
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    pass
# ==============================================================================

DEFAULT_ENTRY_SCRIPT = "files_report_gui.py"


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def show_error_box(title: str, msg: str):
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror(title, msg, parent=root)
        root.destroy()
    except Exception:
        pass


def main():
    # 防止 PyInstaller -w 无控制台模式下 print() 遇到 sys.stdout 为 None 崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    base_dir = get_base_dir()
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    # 判断第一个参数是否是指定的 .py 脚本（如 collectcode.exe http_ui.py）
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".py"):
        target_script = sys.argv[1]
        if not os.path.isabs(target_script):
            target_script = os.path.join(base_dir, target_script)
        sys.argv = sys.argv[1:]
    else:
        # 默认启动同目录下的 files_report_gui.py，并保留后面所有的命令行参数（如 --gen, --copy 等）
        target_script = os.path.join(base_dir, DEFAULT_ENTRY_SCRIPT)
        sys.argv = [target_script] + sys.argv[1:]

    if not os.path.exists(target_script):
        show_error_box(
            "缺少入口脚本",
            f"未找到要运行的脚本文件：\n{target_script}\n\n请确保 {os.path.basename(target_script)} 与本 exe 放在同一文件夹下！",
        )
        return

    try:
        runpy.run_path(target_script, run_name="__main__")
    except SystemExit:
        pass
    except Exception:
        err_detail = traceback.format_exc()
        show_error_box(f"运行 {os.path.basename(target_script)} 出错", err_detail)


if __name__ == "__main__":
    main()
# --- END OF FILE launcher.py ---