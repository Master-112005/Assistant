from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import settings, state
from core.file_index import FileIndex
from core.file_search import SmartFileSearch
from core.files import FileManager
from core.path_resolver import PathResolver


@pytest.fixture
def search_env(tmp_path):
    settings.reset_defaults()
    state.file_index_ready = False

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()

    known_locations = {
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "pictures": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
    }
    for folder in known_locations.values():
        folder.mkdir(parents=True, exist_ok=True)

    resolver = PathResolver(base_dir=workspace, user_home=home, known_locations=known_locations)
    manager = FileManager(path_resolver=resolver, opener=lambda _path: None, trash_func=lambda _path: None)
    index = FileIndex(db_path=tmp_path / "file_index.db")
    search = SmartFileSearch(
        file_manager=manager,
        path_resolver=resolver,
        file_index=index,
        max_files_examined=100000,
    )

    yield SimpleNamespace(
        home=home,
        workspace=workspace,
        known_locations=known_locations,
        resolver=resolver,
        manager=manager,
        index=index,
        search=search,
    )

    settings.reset_defaults()


def test_file_index_build_and_remove_missing(search_env):
    target = search_env.known_locations["documents"] / "report.xlsx"
    target.write_text("budget", encoding="utf-8")

    build_result = search_env.index.build_index(search_env.known_locations.values())

    assert build_result["files"] >= 1
    assert search_env.index.stats()["files"] >= 1

    target.unlink()

    removed = search_env.index.remove_missing(paths=[search_env.known_locations["documents"]])

    assert removed == 1
    assert search_env.index.stats()["files"] == 0


def test_exact_match_outranks_fuzzy_match(search_env):
    exact = search_env.known_locations["documents"] / "resume.pdf"
    fuzzy = search_env.known_locations["documents"] / "old_resume_backup.pdf"
    exact.write_text("resume", encoding="utf-8")
    fuzzy.write_text("old", encoding="utf-8")

    search_env.index.build_index([search_env.known_locations["documents"]])
    state.file_index_ready = True

    response = search_env.search.search("find resume pdf")

    assert response.results
    assert response.results[0].name == "resume.pdf"
    assert response.results[0].score > response.results[1].score


def test_show_excel_files_in_documents_filters_scope(search_env):
    wanted = search_env.known_locations["documents"] / "budget.xlsx"
    ignored = search_env.known_locations["downloads"] / "budget.xlsx"
    wanted.write_text("doc", encoding="utf-8")
    ignored.write_text("download", encoding="utf-8")

    response = search_env.search.search("show excel files in documents")

    assert response.results
    assert all(Path(item.path).parent == search_env.known_locations["documents"] for item in response.results)
    assert response.results[0].name == "budget.xlsx"


def test_latest_file_on_desktop_returns_newest_result(search_env):
    old_file = search_env.known_locations["desktop"] / "notes.txt"
    new_file = search_env.known_locations["desktop"] / "todo.txt"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")

    old_mtime = 1_700_000_000
    new_mtime = old_mtime + 3600
    os.utime(old_file, (old_mtime, old_mtime))
    os.utime(new_file, (new_mtime, new_mtime))

    response = search_env.search.search("latest file on desktop")

    assert response.results
    assert response.results[0].name == "todo.txt"


def test_background_index_retries_locked_database(search_env):
    calls = {"count": 0}

    def flaky_update_index(_roots):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"roots": 1, "files": 1, "removed": 0}

    search_env.search._index.update_index = flaky_update_index
    settings.set("file_index_retry_attempts", 2)
    settings.set("file_index_retry_delay_seconds", 0.01)
    state.file_index_ready = False

    search_env.search.ensure_index_ready_async(force=True)
    thread = search_env.search._index_thread
    assert thread is not None
    thread.join(timeout=2.0)

    assert calls["count"] == 2
    assert state.file_index_ready is True


def test_file_index_update_is_safe_under_concurrent_calls(search_env):
    folder = search_env.known_locations["documents"]
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (folder / "b.txt").write_text("b", encoding="utf-8")
    errors: list[Exception] = []

    def worker() -> None:
        try:
            search_env.index.update_index([folder])
        except Exception as exc:  # pragma: no cover - should not happen
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert not errors
    assert search_env.index.stats()["files"] >= 2


def test_only_one_background_index_worker_runs_globally(search_env, tmp_path):
    started = threading.Event()
    release = threading.Event()

    original_update = search_env.search._index.update_index

    def blocking_update(roots):
        started.set()
        release.wait(timeout=2.0)
        return original_update(roots)

    search_env.search._index.update_index = blocking_update
    settings.set("use_file_index", True)
    state.file_index_ready = False

    home2 = tmp_path / "home2"
    work2 = tmp_path / "work2"
    home2.mkdir()
    work2.mkdir()
    known2 = {k: (home2 / v.name) for k, v in search_env.known_locations.items()}
    for folder in known2.values():
        folder.mkdir(parents=True, exist_ok=True)
    resolver2 = PathResolver(base_dir=work2, user_home=home2, known_locations=known2)
    manager2 = FileManager(path_resolver=resolver2, opener=lambda _path: None, trash_func=lambda _path: None)
    search2 = SmartFileSearch(
        file_manager=manager2,
        path_resolver=resolver2,
        file_index=FileIndex(db_path=tmp_path / "file_index2.db"),
    )

    search_env.search.ensure_index_ready_async(force=True)
    assert started.wait(timeout=1.0)
    search2.ensure_index_ready_async(force=True)
    assert search2._index_thread is None

    release.set()
    thread = search_env.search._index_thread
    assert thread is not None
    thread.join(timeout=3.0)
