#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, DataTable, Static
from textual.timer import Timer


# ==============================
#  App Metadata
# ==============================
__version__ = "dev"


# ==============================
#  Ignore & Filters
# ==============================
IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".mypy_cache",
    "__pycache__",
    ".ipynb_checkpoints",
}

SEARCH_EXTS = {".py", ".ipynb"}   # 改変せず（必要なら変えられる）
MAX_MATCHES = 1000


# ==============================
#  Binary File Check
# ==============================
def is_binary_file(path: Path, blocksize: int = 1024) -> bool:
    """簡易バイナリ判定: 先頭 blocksize バイト中に NUL があればバイナリとみなす。"""
    try:
        with path.open("rb") as f:
            chunk = f.read(blocksize)
            if not chunk:
                return False
            return b"\0" in chunk
    except OSError:
        # 読めないファイルは一旦バイナリ扱いにして除外
        return True


# ==============================
#  Size Formatter
# ==============================
def format_size(size: int) -> str:
    """バイト数を B / kB / MB / GB / TB などに整形して返す."""
    units = ["B", "kB", "MB", "GB", "TB", "PB"]
    s = float(size)
    for unit in units:
        if s < 1024.0 or unit == units[-1]:
            # 小数1桁まで。整数なら .0 を落とす
            if s >= 10 or s.is_integer():
                return f"{s:.0f}{unit}" if s.is_integer() else f"{s:.1f}{unit}"
            else:
                return f"{s:.1f}{unit}"
        s /= 1024.0


# ==============================
#  Data Model
# ==============================
@dataclass
class Match:
    path: Path
    lineno: int
    line: str
    mtime: float
    size: int


# ==============================
#  UI Application
# ==============================
class ScopeFindApp(App):
    """Textual based TUI incremental code search tool."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #dir_label {
        height: 1;
        padding: 0 1;
    }

    #toolbar {
        height: 1;
        padding: 0 1;
    }

    #status {
        height: 1;
        padding: 0 1;
    }

    #pattern_input {
        height: 3;
        padding: 0 1;
        border: solid $accent;
        content-align: left middle;
    }

    #results {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("f2", "sort_name", "Sort by name"),
        ("f3", "sort_date", "Sort by date"),
        ("f4", "sort_size", "Sort by size"),
        ("f5", "toggle_py", "Toggle .py"),
        ("f6", "toggle_binary", "Toggle binary"),
        ("/", "focus_search", "Focus search"),
        ("q", "quit", "Quit"),
        ("enter", "run_search", "Run search"),  # ← Enter で検索実行
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
    ]

    def __init__(self, start_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.start_dir = start_dir
        self.pattern: str = ""
        self.include_py: bool = True
        self.include_binary: bool = False
        self.sort_key: str = "name"
        self.matches: List[Match] = []
        self.file_candidates: List[Path] = []  # ← 検索対象キャッシュ
        self._search_timer: Optional[Timer] = None  # ← デバウンス用

    # --------------- UI Layout ---------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Dir: {self.start_dir}", id="dir_label")
        yield Input(placeholder="Search pattern (literal match)", id="pattern_input")
        yield Static("", id="toolbar")
        yield Static("Enter pattern to search.", id="status")
        yield DataTable(id="results")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns("#", "Path", "Line", "Preview", "Size", "Modified")
        table.cursor_type = "row"
        table.zebra_stripes = True

        self.build_file_candidates()   # ← 最初に一度だけ探索
        self.update_toolbar()
        self.update_status("Enter pattern to search.")

    # --------------- UI Helpers ---------------

    def update_toolbar(self) -> None:
        sort_name = {"name": "NAME", "date": "DATE", "size": "SIZE"}[self.sort_key]
        py_label = "ON" if self.include_py else "OFF"
        bin_label = "ON" if self.include_binary else "OFF"
        toolbar = (
            f"Sort: {sort_name} | "
            f".py: {py_label} | "
            f"Binary: {bin_label} | "
            f"F2/F3/F4: sort, F5/F6: filter, '/': search, Enter: search, q: quit"
        )
        self.query_one("#toolbar", Static).update(toolbar)

    def update_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def refresh_table(self) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        if not self.matches:
            return

        for idx, m in enumerate(self.matches, start=1):
            relpath = str(m.path.relative_to(self.start_dir))
            preview = m.line.replace("\t", "    ")
            if len(preview) > 120:
                preview = preview[:117] + "..."
            # ここを変更：バイト数を人間向け表示にする
            size_str = format_size(m.size)
            dt_str = datetime.fromtimestamp(m.mtime).strftime("%Y-%m-%d %H:%M")
            table.add_row(str(idx), relpath, str(m.lineno), preview, size_str, dt_str)

    # --------------- File Candidate Build (Caching) ---------------

    def build_file_candidates(self) -> None:
        candidates: List[Path] = []
        for root, dirs, files in os.walk(self.start_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for name in files:
                path = Path(root) / name

                # Binary OFF → バイナリは除外
                if not self.include_binary and is_binary_file(path):
                    continue

                candidates.append(path)

        self.file_candidates = candidates

    # --------------- Search Logic ---------------

    def run_search(self) -> None:
        pat = self.pattern
        if not pat:
            self.matches = []
            self.update_status("Enter pattern to search.")
            self.refresh_table()
            return

        matches: List[Match] = []
        total_files = len(self.file_candidates)
        used_files = 0

        for path in self.file_candidates:
            ext = path.suffix
            if ext == ".py" and not self.include_py:
                continue

            used_files += 1

            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, start=1):
                        if pat in line:
                            st = path.stat()
                            matches.append(
                                Match(
                                    path=path,
                                    lineno=lineno,
                                    line=line.rstrip("\n"),
                                    mtime=st.st_mtime,
                                    size=st.st_size,
                                )
                            )
                            if len(matches) >= MAX_MATCHES:
                                break
                if len(matches) >= MAX_MATCHES:
                    break
            except (OSError, UnicodeError):
                continue

        self.matches = matches
        self.sort_matches()

        if matches:
            extra = f" (truncated to {MAX_MATCHES})" if len(matches) >= MAX_MATCHES else ""
            self.update_status(
                f"Pattern '{pat}' : {len(matches)} matches in {used_files}/{total_files} files{extra}."
            )
        else:
            self.update_status(f"No matches for: '{pat}'")

        self.refresh_table()

    # --------------- Debounce Input Change ---------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "pattern_input":
            return

        self.pattern = event.value

        # 既存タイマーをキャンセル
        if self._search_timer is not None:
            self._search_timer.stop()

        # 1秒入力が止まったら検索
        self._search_timer = self.set_timer(1, self._debounced_search)

    def _debounced_search(self) -> None:
        self.run_search()

    # --------------- Sort ---------------

    def sort_matches(self) -> None:
        if not self.matches:
            return

        if self.sort_key == "name":
            self.matches.sort(key=lambda m: (str(m.path), m.lineno))
        elif self.sort_key == "date":
            self.matches.sort(key=lambda m: (m.mtime, str(m.path)), reverse=True)
        elif self.sort_key == "size":
            self.matches.sort(key=lambda m: (m.size, str(m.path)), reverse=True)

    # --------------- Key Actions ---------------

    def action_run_search(self) -> None:
        self.pattern = self.query_one("#pattern_input", Input).value
        self.run_search()

    def action_focus_search(self) -> None:
        self.query_one("#pattern_input", Input).focus()

    def action_sort_name(self) -> None:
        self.sort_key = "name"
        self.update_toolbar()
        self.sort_matches()
        self.refresh_table()

    def action_sort_date(self) -> None:
        self.sort_key = "date"
        self.update_toolbar()
        self.sort_matches()
        self.refresh_table()

    def action_sort_size(self) -> None:
        self.sort_key = "size"
        self.update_toolbar()
        self.sort_matches()
        self.refresh_table()

    def action_toggle_py(self) -> None:
        self.include_py = not self.include_py
        self.update_toolbar()
        self.run_search()

    def action_toggle_binary(self) -> None:
        self.include_binary = not self.include_binary
        self.update_toolbar()
        self.build_file_candidates()  # ← バイナリフィルタが変わるので再構築
        self.run_search()

    def action_cursor_down(self) -> None:
        table = self.query_one("#results", DataTable)
        try:
            table.action_cursor_down()
        except Exception:
            pass

    def action_cursor_up(self) -> None:
        table = self.query_one("#results", DataTable)
        try:
            table.action_cursor_up()
        except Exception:
            pass


# ==============================
#  CLI Entry Point
# ==============================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ScopeFind: Fast incremental code search tool (Python + Textual)."
    )
    parser.add_argument(
        "start_dir",
        nargs="?",
        default=".",
        help="Search start directory (default: current directory)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version information and exit",
    )
    args = parser.parse_args()

    if args.version:
        print(f"ScopeFind v{__version__}")
        sys.exit(0)

    start_dir = Path(args.start_dir).resolve()
    if not start_dir.is_dir():
        print(f"Not a directory: {start_dir}", file=sys.stderr)
        sys.exit(1)

    app = ScopeFindApp(start_dir=start_dir)
    app.run()


if __name__ == "__main__":
    main()
