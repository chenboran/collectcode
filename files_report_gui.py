# -*- coding: utf-8 -*-
# 依赖安装: python.exe Scripts\pip.exe install chardet reportlab tkinterdnd2
#
# 功能：
#   - 通过按钮添加文件 / 文件夹（可多次添加不同的文件夹和文件）
#   - 支持鼠标直接把文件/文件夹拖拽到窗口里添加
#   - 自定义"限制条目"（排除规则），支持通配符，比如 *.log、node_modules、.git
#   - 所有条目（文件/文件夹/排除规则）自动保存到 exe 同目录下的 config.json，下次打开自动加载
#   - 点击"生成合集"后，遍历条目中所有子目录，把命中的文件内容汇总，
#     在 exe 所在目录生成 combined_files_report.txt 和 .pdf

import os
import sys
import json
import fnmatch
import threading
import queue
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

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


# ================= 默认配置 =================
DEFAULT_MAX_FILE_SIZE = 1 * 1024 * 1024   # 1MB，超过此大小的文件会被截断
DEFAULT_TRUNCATE_SIZE = 50 * 1024         # 50KB，截取前面这么多字符
DEFAULT_EXCLUDES = ["__pycache__", ".git", ".vs", "node_modules", "*.exe", "*.dll"]
# ============================================


def get_app_dir():
    """获取 exe（或脚本）所在目录，配置文件和生成结果都放这里"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(get_app_dir(), "config.json")


def load_config():
    default = {
        "entries": [],
        "excludes": DEFAULT_EXCLUDES.copy(),
        "max_file_size": DEFAULT_MAX_FILE_SIZE,
        "truncate_size": DEFAULT_TRUNCATE_SIZE,
    }
    if not os.path.exists(CONFIG_PATH):
        return default
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_config(entries, excludes, max_file_size, truncate_size):
    data = {
        "entries": entries,
        "excludes": excludes,
        "max_file_size": max_file_size,
        "truncate_size": truncate_size,
    }
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存配置失败: {e}")


# ================= 文件处理相关（与原逻辑一致，只是改成支持多条目+排除规则）=================

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


def process_entries(entries, exclude_patterns, max_file_size, truncate_size, log_fn=print):
    """遍历所有条目，生成合集文本"""
    combined_text = []
    file_count = 0

    for root_label, rel_path, full_path in iter_files_from_entries(entries, exclude_patterns):
        file_count += 1
        try:
            file_size = os.path.getsize(full_path)
        except OSError:
            continue

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

    return "".join(combined_text), file_count


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
        log_fn(f"PDF生成成功: {output_file}")
        return True

    except Exception as e:
        log_fn(f"转换过程中出现错误: {str(e)}")
        return False


# ================= 图形界面 =================

BaseTk = TkinterDnD.Tk if HAS_DND else tk.Tk


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("文件内容合集生成工具")
        self.geometry("760x620")
        self.minsize(640, 520)

        cfg = load_config()
        self.entries = cfg["entries"]
        self.excludes = cfg["excludes"]
        self.max_file_size = tk.IntVar(value=cfg["max_file_size"])
        self.truncate_size = tk.IntVar(value=cfg["truncate_size"])

        self.log_queue = queue.Queue()
        self._build_ui()
        self._refresh_entries_list()
        self._refresh_excludes_list()
        self._poll_log_queue()

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
        main.rowconfigure(1, weight=1)

        # --- 条目列表 ---
        entries_frame = ttk.LabelFrame(main, text="要处理的文件 / 文件夹（可拖拽添加）")
        entries_frame.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 5))
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

        # --- 排除规则列表 ---
        excl_frame = ttk.LabelFrame(main, text="限制条目 / 排除规则（支持通配符，如 *.log）")
        excl_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
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

        # --- 限制参数 ---
        opts_frame = ttk.LabelFrame(main, text="单文件大小限制")
        opts_frame.grid(row=1, column=1, sticky="new", padx=(5, 0), pady=(8, 0))
        ttk.Label(opts_frame, text="超过多少 KB 视为大文件:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(opts_frame, textvariable=self.max_file_size, width=12).grid(row=0, column=1, padx=4, pady=4)
        ttk.Label(opts_frame, text="大文件截取前多少字节:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(opts_frame, textvariable=self.truncate_size, width=12).grid(row=1, column=1, padx=4, pady=4)
        ttk.Label(opts_frame, text="(单位：字节，1MB=1048576)").grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

        # --- 生成按钮 + 日志 ---
        bottom = ttk.Frame(main)
        bottom.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        bottom.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        self.gen_btn = ttk.Button(bottom, text="生成合集", command=self.start_generate)
        self.gen_btn.pack(anchor="w", pady=(0, 6))

        self.log_box = scrolledtext.ScrolledText(bottom, height=14, state="disabled")
        self.log_box.pack(fill=tk.BOTH, expand=True)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 条目操作 ----------
    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择一个或多个文件")
        for p in paths:
            self._add_entry(p)
        self._save()

    def add_folder(self):
        # askdirectory 一次只能选一个，用户可以多次点击来加多个不同文件夹
        path = filedialog.askdirectory(title="选择一个文件夹（可重复点击本按钮添加多个）")
        if path:
            self._add_entry(path)
            self._save()

    def on_drop(self, event):
        # tkinterdnd2 返回的字符串需要用 splitlist 解析（路径可能包含空格）
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
        if self.entries and messagebox.askyesno("确认", "清空所有已添加的文件/文件夹？"):
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
        save_config(self.entries, self.excludes, self.max_file_size.get(), self.truncate_size.get())

    def on_close(self):
        self._save()
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

    # ---------- 生成合集 ----------
    def start_generate(self):
        if not self.entries:
            messagebox.showwarning("提示", "请先添加至少一个文件或文件夹")
            return
        self._save()
        self.gen_btn.configure(state="disabled", text="生成中...")
        threading.Thread(target=self._generate_worker, daemon=True).start()

    def _generate_worker(self):
        try:
            app_dir = get_app_dir()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            txt_path = os.path.join(app_dir, f"combined_files_report_{timestamp}.txt")
            pdf_path = os.path.join(app_dir, f"combined_files_report_{timestamp}.pdf")

            self._log(f"开始处理，共 {len(self.entries)} 个条目...")
            combined_text, file_count = process_entries(
                self.entries,
                self.excludes,
                self.max_file_size.get(),
                self.truncate_size.get(),
                log_fn=self._log,
            )

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(combined_text)
            self._log(f"文本合集已生成: {txt_path}（共 {file_count} 个文件）")

            text_to_pdf(txt_path, pdf_path, log_fn=self._log)

            self._log("全部完成！")
            self.after(0, lambda: messagebox.showinfo(
                "完成", f"合集已生成：\n{txt_path}\n{pdf_path}"
            ))
        except Exception as e:
            self._log(f"发生错误: {e}")
            self.after(0, lambda: messagebox.showerror("错误", str(e)))
        finally:
            self.after(0, lambda: self.gen_btn.configure(state="normal", text="生成合集"))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()