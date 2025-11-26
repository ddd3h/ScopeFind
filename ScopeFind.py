#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Header, Footer, Input, DataTable, Static

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".mypy_cache",
    "__pycache__",
    ".ipynb_checkpoints",
}

SEARCH_EXTS = {".py", ".ipynb"}
MAX_MATCHES = 1000


def is_binary_file(path: Path, blocksize: int = 1024) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(blocksize)
            if not chunk:
                return False
            return b"\0" in chunk
    except OSError:
        return True


@dataclass
class Match:
    path: Path
    lineno: int
    line: str
    mtime: float
    size: int


class CodeSearchApp(App):
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Dir: {self.start_dir}", id="dir_label")
        yield Input(placeholder="Search pattern (literal match, not regex)", id="pattern_input")
        yield Static("", id="toolbar")
        yield Static("Enter pattern to search.", id="status")
        yield DataTable(id="results")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns("#", "Path", "Line", "Preview", "Size", "Modified")
        table.cursor_type = "row"
        table.zebra_stripes = True

        self.update_toolbar()
        self.update_status("Enter pattern to search.")

    def update_toolbar(self) -> None:
        sort_name = {"name": "NAME", "date": "DATE", "size": "SIZE"}[self.sort_key]
        py_label = "ON" if self.include_py else "OFF"
        bin_label = "ON" if self.include_binary else "OFF"
        toolbar = (
            f"Sort: {sort_name} | "
            f".py: {py_label} | "
            f"Binary: {bin_label} | "
            f"F2/F3/F4: sort, F5/F6: filter, '/': focus search, q: quit"
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
            size_str = str(m.size)
            dt_str = datetime.fromtimestamp(m.mtime).strftime("%Y-%m-%d %H:%M")
            table.add_row(str(idx), relpath, str(m.lineno), preview, size_str, dt_str)

    def run_search(self) -> None:
        pat = self.pattern
        if not pat:
            self.matches = []
            self.update_status("Enter pattern to search.")
            self.refresh_table()
            return

        matches: List[Match] = []
        total_files = 0
        used_files = 0

        for root, dirs, files in os.walk(self.start_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

            for name in files:
                total_files += 1
                path = Path(root) / name

                ext = path.suffix
                if ext not in SEARCH_EXTS:
                    continue
                if ext == ".py" and not self.include_py:
                    continue

                if not self.include_binary and is_binary_file(path):
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

            if len(matches) >= MAX_MATCHES:
                break

        self.matches = matches
        self.sort_matches()

        if matches:
            extra = ""
            if len(matches) >= MAX_MATCHES:
                extra = f" (truncated to {MAX_MATCHES})"
            self.update_status(
                f"Pattern '{pat}' : {len(matches)} matches "
                f"in {used_files}/{total_files} files{extra}."
            )
        else:
            self.update_status(f"No matches for: '{pat}'")

        self.refresh_table()

    def sort_matches(self) -> None:
        if not self.matches:
            return

        if self.sort_key == "name":
            self.matches.sort(key=lambda m: (str(m.path), m.lineno))
        elif self.sort_key == "date":
            self.matches.sort(key=lambda m: (m.mtime, str(m.path)), reverse=True)
        elif self.sort_key == "size":
            self.matches.sort(key=lambda m: (m.size, str(m.path)), reverse=True)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pattern_input":
            self.pattern = event.value
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Textual-based TUI code search (.py, .ipynb)."
    )
    parser.add_argument(
        "start_dir",
        nargs="?",
        default=".",
        help="Search start directory (default: current directory)",
    )
    args = parser.parse_args()

    start_dir = Path(args.start_dir).resolve()
    if not start_dir.is_dir():
        print(f"Not a directory: {start_dir}", file=sys.stderr)
        sys.exit(1)

    app = CodeSearchApp(start_dir=start_dir)
    app.run()


if __name__ == "__main__":
    main()

