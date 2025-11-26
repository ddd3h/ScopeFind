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

from rich.text import Text

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, DataTable, Static
from textual.timer import Timer

from multiprocessing import Process, Queue
import queue as queue_mod


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
    "node_modules",
    "dist",
    "build",
    ".cache",
}

# .py トグル ON のときに対象にする拡張子
SEARCH_EXTS = {".py", ".ipynb"}

# 検索結果の最大数
MAX_MATCHES = 300

# プレビューの最大表示文字数（短め）
MAX_PREVIEW_CHARS = 80

# Binary ON のときに無視するファイルサイズの閾値（2MB）
BINARY_MAX_SIZE = 2 * 1024 * 1024  # 2MB

# 「テキストとして扱う拡張子」のホワイトリスト
TEXT_EXTS = {
    # --- ソースコード系 ---
    ".py", ".ipynb",
    ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cu",
    ".java",
    ".js", ".ts", ".jsx", ".tsx",
    ".rs",
    ".go",
    ".rb",
    ".php",
    ".sh", ".bash", ".zsh", ".fish",
    ".pl",
    ".r", ".jl", ".m",
    ".rpy",
    ".ps1", ".bat", ".cmd",
    ".awk",
    ".cmake",
    ".gnuplot",
    ".proto",
    ".gradle", ".groovy",

    # --- ドキュメント / マークアップ / 設定 ---
    ".txt",
    ".md", ".mkd", ".markdown", ".td",
    ".rst",
    ".adoc", ".creole",
    ".tex", ".sty", ".cls",
    ".bib",
    ".ini", ".cfg", ".conf", ".cnf",
    ".yaml", ".yml",
    ".toml",
    ".json",
    ".env", ".dotenv",
    ".properties",
    ".schema",
    ".service",
    ".desktop",
    ".gitattributes", ".gitignore", ".editorconfig",
    ".lock",
    ".log",
    ".dat", ".lst", ".out", ".err",

    # --- Web / テンプレート / UI ---
    ".xml",
    ".html", ".htm",
    ".css", ".scss", ".sass", ".less",
    ".csv", ".tsv",
    ".vue", ".svelte",
    ".twig",
    ".jinja", ".jinja2",
    ".ejs",
    ".mustache", ".handlebars", ".hbs",
    ".map",
    ".sql",

    # --- その他テキストなデータ形式 ---
    ".tmx",
    ".gltf",
    ".zep",
}


# ==============================
#  Size Formatter
# ==============================
def format_size(size: int) -> str:
    """バイト数を B / kB / MB / GB / TB などに整形して返す."""
    units = ["B", "kB", "MB", "GB", "TB", "PB"]
    s = float(size)
    for unit in units:
        if s < 1024.0 or unit == units[-1]:
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
#  Worker Process
# ==============================
def search_worker_process(
    start_dir_str: str,
    pattern: str,
    include_py: bool,
    include_binary: bool,
    max_matches: int,
    binary_max_size: int,
    ignore_dirs: set,
    search_exts: set,
    text_exts: set,
    result_queue: Queue,
) -> None:
    """
    別プロセスで実行される検索処理。
    メインプロセスとは Queue 経由で通信する。
    """
    start_dir = Path(start_dir_str)

    # ----- まず総ファイル数を数える（進捗分母用） -----
    total_files = 0
    for root, dirs, files in os.walk(start_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        total_files += len(files)

    used_files = 0
    match_count = 0
    skipped_large = 0

    # 初期 progress
    if total_files == 0:
        result_queue.put(
            {
                "type": "progress",
                "used_files": 0,
                "total_files": 0,
                "match_count": 0,
                "skipped_large": 0,
            }
        )
        result_queue.put(
            {
                "type": "done",
                "used_files": 0,
                "total_files": 0,
                "match_count": 0,
                "skipped_large": 0,
                "truncated": False,
            }
        )
        return

    result_queue.put(
        {
            "type": "progress",
            "used_files": 0,
            "total_files": total_files,
            "match_count": 0,
            "skipped_large": 0,
        }
    )

    matches_batch = []
    BATCH_SIZE = 10
    PROGRESS_EVERY = 20  # 20ファイルごとに進捗通知

    truncated = False

    for root, dirs, files in os.walk(start_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]

        for name in files:
            if match_count >= max_matches:
                truncated = True
                break

            path = Path(root) / name
            suffix = path.suffix.lower()
            skip = False

            if include_py:
                # .py トグル ON → .py / .ipynb のみ
                if suffix not in search_exts:
                    skip = True
            else:
                if not include_binary:
                    # .py OFF & Binary OFF → TEXT_EXTS のみ
                    if suffix not in text_exts:
                        skip = True
                else:
                    # .py OFF & Binary ON → すべての拡張子を候補に含めるが、
                    # 大きすぎるファイル（>2MB）はスキップ
                    try:
                        st = path.stat()
                        if st.st_size > binary_max_size:
                            skipped_large += 1
                            skip = True
                    except OSError:
                        skip = True

            used_files += 1

            if skip:
                if used_files % PROGRESS_EVERY == 0:
                    result_queue.put(
                        {
                            "type": "progress",
                            "used_files": used_files,
                            "total_files": total_files,
                            "match_count": match_count,
                            "skipped_large": skipped_large,
                        }
                    )
                continue

            try:
                st = path.stat()
                mtime = st.st_mtime
                size = st.st_size

                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, start=1):
                        if pattern in line:
                            matches_batch.append(
                                {
                                    "path": str(path),
                                    "lineno": lineno,
                                    "line": line.rstrip("\n"),
                                    "mtime": mtime,
                                    "size": size,
                                }
                            )
                            match_count += 1

                            # 一定数たまったら UI に送信
                            if len(matches_batch) >= BATCH_SIZE:
                                result_queue.put(
                                    {"type": "matches", "items": matches_batch}
                                )
                                matches_batch = []

                            if match_count >= max_matches:
                                truncated = True
                                break
            except (OSError, UnicodeError):
                # 読めないファイルは無視
                pass

            if used_files % PROGRESS_EVERY == 0:
                result_queue.put(
                    {
                        "type": "progress",
                        "used_files": used_files,
                        "total_files": total_files,
                        "match_count": match_count,
                        "skipped_large": skipped_large,
                    }
                )

        if match_count >= max_matches:
            break

    # 余りのバッチを送る
    if matches_batch:
        result_queue.put({"type": "matches", "items": matches_batch})

    # 最終 progress & done
    result_queue.put(
        {
            "type": "progress",
            "used_files": used_files,
            "total_files": total_files,
            "match_count": match_count,
            "skipped_large": skipped_large,
        }
    )

    result_queue.put(
        {
            "type": "done",
            "used_files": used_files,
            "total_files": total_files,
            "match_count": match_count,
            "skipped_large": skipped_large,
            "truncated": truncated,
        }
    )


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

    #progress {
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
        ("enter", "run_search", "Run search"),
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
        self._search_timer: Optional[Timer] = None  # デバウンス用

        # 検索状態管理
        self._current_search_id: int = 0

        # プロセスベースの検索ワーカー
        self._worker_proc: Optional[Process] = None
        self._worker_queue: Optional[Queue] = None

        # 進捗・結果（プロセスからのメッセージで更新）
        self._search_in_progress: bool = False
        self._progress_used_files: int = 0
        self._progress_total_files: int = 0
        self._progress_match_count: int = 0
        self._skipped_large_files: int = 0

        self._current_pattern_for_status: str = ""

    # --------------- UI Layout ---------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Dir: {self.start_dir}", id="dir_label")
        yield Input(placeholder="Search pattern (literal match)", id="pattern_input")
        yield Static("", id="toolbar")
        yield Static("Enter pattern to search.", id="status")
        yield Static("", id="progress")   # プログレス表示用
        yield DataTable(id="results")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns("#", "Path", "Line", "Preview", "Size", "Modified")
        table.cursor_type = "row"
        table.zebra_stripes = True

        self.update_toolbar()
        self.update_status("Enter pattern to search.")
        self.update_progress(0, 0, 0)

        # プロセスからのメッセージと進捗をポーリングするタイマー
        self.set_interval(0.1, self._progress_tick)

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

    def update_progress(self, used_files: int, total_files: int, match_count: int) -> None:
        """進捗バーと簡易情報を表示する。"""
        progress_widget = self.query_one("#progress", Static)

        # total_files > 0 のときは常に「最後の状態」を出す
        if total_files <= 0:
            progress_widget.update("")
            return

        percent = int(used_files * 100 / total_files) if total_files else 0
        bar_width = 30
        filled = int(bar_width * percent / 100)
        bar = "█" * filled + " " * (bar_width - filled)

        text = (
            f"[{bar}] {percent:3d}%  "
            f"files: {used_files}/{total_files}  "
            f"matches: {match_count}"
        )

        if self._skipped_large_files and self.include_binary and not self.include_py:
            text += f"  skipped>2MB: {self._skipped_large_files}"

        progress_widget.update(text)

    def refresh_table(self) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        if not self.matches:
            return

        def T_(s: str) -> Text:
            return Text(s, no_wrap=True, end="")

        for idx, m in enumerate(self.matches, start=1):
            try:
                relpath = str(m.path.relative_to(self.start_dir))
            except ValueError:
                relpath = str(m.path)
            preview = m.line.replace("\t", "    ")
            if len(preview) > MAX_PREVIEW_CHARS:
                preview = preview[: MAX_PREVIEW_CHARS - 3] + "..."
            size_str = format_size(m.size)
            dt_str = datetime.fromtimestamp(m.mtime).strftime("%Y-%m-%d %H:%M")
            table.add_row(
                T_(str(idx)),
                T_(relpath),
                T_(str(m.lineno)),
                T_(preview),
                T_(size_str),
                T_(dt_str),
            )

    # --------------- Worker Process Management ---------------

    def _terminate_worker(self) -> None:
        """現在の検索プロセスを強制終了（あれば）"""
        if self._worker_proc is not None:
            try:
                if self._worker_proc.is_alive():
                    self._worker_proc.terminate()  # ★ 強制 kill ★
                    self._worker_proc.join(timeout=0.1)
            except Exception:
                pass
        self._worker_proc = None
        self._worker_queue = None
        self._search_in_progress = False

    def _cleanup_worker(self) -> None:
        """正常終了後のプロセス片付け"""
        if self._worker_proc is not None:
            try:
                if not self._worker_proc.is_alive():
                    self._worker_proc.join(timeout=0.1)
            except Exception:
                pass
        self._worker_proc = None
        self._worker_queue = None

    # --------------- Search Logic (process-based) ---------------

    def run_search(self) -> None:
        """検索を開始する。前の検索は強制終了する。"""
        pat = self.pattern

        # まず前回の検索を kill
        self._terminate_worker()

        if not pat:
            # パターンが空なら何もしない（結果はクリア）
            self.matches = []
            self._progress_used_files = 0
            self._progress_total_files = 0
            self._progress_match_count = 0
            self._skipped_large_files = 0
            self.update_status("Enter pattern to search.")
            self.update_progress(0, 0, 0)
            self.refresh_table()
            return

        # 新しい検索 ID（今は UI 側では使っていないが、将来用）
        self._current_search_id += 1

        self._current_pattern_for_status = pat
        self.matches = []
        self._progress_used_files = 0
        self._progress_total_files = 0
        self._progress_match_count = 0
        self._skipped_large_files = 0
        self._search_in_progress = True
        self.update_status(f"Searching for '{pat}' ...")

        # Queue と Process を起動
        q: Queue = Queue()
        self._worker_queue = q

        proc = Process(
            target=search_worker_process,
            args=(
                str(self.start_dir),
                pat,
                self.include_py,
                self.include_binary,
                MAX_MATCHES,
                BINARY_MAX_SIZE,
                IGNORE_DIRS,
                SEARCH_EXTS,
                TEXT_EXTS,
                q,
            ),
        )
        proc.daemon = True
        proc.start()
        self._worker_proc = proc

    def _progress_tick(self) -> None:
        """プロセスからのメッセージを処理しつつ、進捗＆完了処理を行う。"""
        # ここでローカルに退避しておくことで、
        # ループ途中で self._worker_queue が None になっても問題ないようにする
        q = self._worker_queue
        if q is None:
            return

        cleanup_needed = False

        while True:
            try:
                msg = q.get_nowait()
            except queue_mod.Empty:
                break

            mtype = msg.get("type")

            if mtype == "progress":
                self._progress_used_files = msg.get("used_files", 0)
                self._progress_total_files = msg.get("total_files", 0)
                self._progress_match_count = msg.get("match_count", 0)
                self._skipped_large_files = msg.get("skipped_large", 0)
                self.update_progress(
                    self._progress_used_files,
                    self._progress_total_files,
                    self._progress_match_count,
                )

            elif mtype == "matches":
                items = msg.get("items", [])
                for item in items:
                    self.matches.append(
                        Match(
                            path=Path(item["path"]),
                            lineno=item["lineno"],
                            line=item["line"],
                            mtime=item["mtime"],
                            size=item["size"],
                        )
                    )
                # 途中でも結果を見たいのでここでテーブル更新
                self.refresh_table()

            elif mtype == "done":
                self._search_in_progress = False
                self._progress_used_files = msg.get("used_files", 0)
                self._progress_total_files = msg.get("total_files", 0)
                self._progress_match_count = msg.get("match_count", 0)
                self._skipped_large_files = msg.get("skipped_large", 0)
                truncated = msg.get("truncated", False)

                # MAX_MATCHES に達して途中打ち切りの場合は、
                # 「ここまで見た」を 100% とみなす
                if truncated and self._progress_total_files > self._progress_used_files:
                    self._progress_total_files = self._progress_used_files

                # 最終ソート
                self.sort_matches()

                pat = self._current_pattern_for_status
                used_files = self._progress_used_files
                total_files = self._progress_total_files
                match_count = self._progress_match_count

                if match_count:
                    extra = f" (truncated to {MAX_MATCHES})" if truncated else ""
                    msg_text = (
                        f"Pattern '{pat}' : {match_count} matches in "
                        f"{used_files}/{total_files} files{extra}."
                    )
                else:
                    msg_text = f"No matches for: '{pat}'"

                if self._skipped_large_files and self.include_binary and not self.include_py:
                    msg_text += f" (skipped {self._skipped_large_files} files >2MB in binary mode)"

                self.update_status(msg_text)

                # 最終状態を反映（必ず実行）
                self.update_progress(
                    self._progress_used_files,
                    self._progress_total_files,
                    self._progress_match_count,
                )
                self.refresh_table()

                # ループの外で _cleanup_worker を呼ぶためのフラグ
                cleanup_needed = True

        if cleanup_needed:
            self._cleanup_worker()

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
        """Toggle .py ボタン。トグルして再検索。"""
        self.include_py = not self.include_py
        self.update_toolbar()
        self.run_search()

    def action_toggle_binary(self) -> None:
        """バイナリ ON/OFF 切り替え。トグルして再検索。"""
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

    async def on_unmount(self) -> None:
        # アプリ終了時にワーカーが残っていたら殺す
        self._terminate_worker()


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
