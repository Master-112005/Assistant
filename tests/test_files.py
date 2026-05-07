from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import settings, state
from core.file_index import FileIndex
from core.file_search import SmartFileSearch
from core.files import FileManager
from core.path_resolver import PathResolver
from core.safety import FileSafetyPolicy
from skills.files import FileSkill


@pytest.fixture
def file_env(tmp_path):
    settings.reset_defaults()
    state.pending_confirmation = {}
    state.last_file_action = ""
    state.last_file_path = ""
    state.last_destination_path = ""
    state.recent_files_touched = []
    state.last_file_search_query = {}
    state.last_file_search_results = []
    state.pending_file_choices = []
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

    opened: list[str] = []
    trashed: list[str] = []

    resolver = PathResolver(base_dir=workspace, user_home=home, known_locations=known_locations)
    manager = FileManager(
        path_resolver=resolver,
        opener=lambda path: opened.append(path),
        trash_func=lambda path: trashed.append(path),
    )
    search = SmartFileSearch(
        file_manager=manager,
        path_resolver=resolver,
        file_index=FileIndex(db_path=tmp_path / "file_index.db"),
        max_files_examined=100000,
    )
    skill = FileSkill(
        file_manager=manager,
        safety_policy=FileSafetyPolicy(path_resolver=resolver),
        smart_search=search,
    )

    yield SimpleNamespace(
        home=home,
        workspace=workspace,
        known_locations=known_locations,
        resolver=resolver,
        manager=manager,
        search=search,
        skill=skill,
        opened=opened,
        trashed=trashed,
    )

    settings.reset_defaults()
    state.pending_confirmation = {}
    state.pending_file_choices = []


def test_path_resolver_handles_special_names_and_prefixes(file_env):
    desktop = file_env.resolver.resolve("desktop")
    note_path = file_env.resolver.resolve("desktop\\notes.txt")

    assert desktop == file_env.known_locations["desktop"].resolve(strict=False)
    assert note_path == (file_env.known_locations["desktop"] / "notes.txt").resolve(strict=False)


def test_file_manager_create_file_and_prevent_unconfirmed_overwrite(file_env):
    result = file_env.manager.create_file(file_env.resolver.resolve("desktop\\notes.txt"))
    collision = file_env.manager.create_file(file_env.resolver.resolve("desktop\\notes.txt"))

    assert result.success is True
    assert (file_env.known_locations["desktop"] / "notes.txt").exists()
    assert collision.success is False
    assert collision.error == "destination_exists"


def test_file_manager_rename_blocks_invalid_name(file_env):
    source = file_env.known_locations["documents"] / "report.txt"
    source.write_text("draft", encoding="utf-8")

    result = file_env.manager.rename_file(source, "bad<name>.txt")

    assert result.success is False
    assert result.error == "invalid_filename"


def test_file_manager_move_handles_name_collision(file_env):
    source = file_env.known_locations["desktop"] / "report.txt"
    destination = file_env.known_locations["documents"] / "report.txt"
    source.write_text("draft", encoding="utf-8")
    destination.write_text("existing", encoding="utf-8")

    result = file_env.manager.move_file(source, file_env.known_locations["documents"])

    assert result.success is False
    assert result.error == "destination_exists"
    assert source.exists()
    assert destination.exists()


def test_file_skill_multiple_matches_requires_user_choice(file_env):
    (file_env.known_locations["desktop"] / "notes.txt").write_text("desktop", encoding="utf-8")
    (file_env.known_locations["documents"] / "notes.txt").write_text("documents", encoding="utf-8")

    first = file_env.skill.execute("open notes.txt", {})

    assert first.success is False
    assert first.error == "multiple_matches"
    assert state.pending_confirmation["kind"] == "choice"
    second = file_env.skill.execute("2", {})
    assert second.success is True
    assert second.intent == "file_open"
    assert file_env.opened == [str((file_env.known_locations["documents"] / "notes.txt").resolve(strict=False))]


def test_file_skill_delete_requires_confirmation_and_then_uses_recycle_bin(file_env):
    source = file_env.known_locations["desktop"] / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    first = file_env.skill.execute("delete notes.txt from desktop", {})

    assert first.success is False
    assert first.error == "confirmation_required"
    assert state.pending_confirmation["kind"] == "confirm"
    second = file_env.skill.execute("yes", {})
    assert second.success is True
    assert second.intent == "file_delete"
    assert file_env.trashed == [str(source.resolve(strict=False))]
    assert state.last_file_action == "delete"


def test_file_skill_create_file_on_desktop(file_env):
    result = file_env.skill.execute("create file todo.txt on desktop", {})

    assert result.success is True
    assert result.intent == "file_create"
    assert (file_env.known_locations["desktop"] / "todo.txt").exists()


def test_file_skill_smart_search_lists_results_and_follow_up_opens_selected_match(file_env):
    (file_env.known_locations["documents"] / "resume.pdf").write_text("resume", encoding="utf-8")
    (file_env.known_locations["documents"] / "resume_final.pdf").write_text("resume", encoding="utf-8")
    file_env.search._index.build_index(file_env.known_locations.values())
    state.file_index_ready = True

    first = file_env.skill.execute("find my resume pdf", {"intent": "search"})

    assert first.success is True
    assert first.intent == "file_search"
    assert "resume.pdf" in first.response
    assert len(state.pending_file_choices) == 2

    second = file_env.skill.execute("open second one", {"intent": "open_app"})

    assert second.success is True
    assert second.intent == "file_open"
    assert file_env.opened == [str((file_env.known_locations["documents"] / "resume_final.pdf").resolve(strict=False))]


def test_file_skill_open_latest_screenshot_opens_best_result(file_env):
    older = file_env.known_locations["pictures"] / "Screenshot_001.png"
    newer = file_env.known_locations["pictures"] / "Screenshot_002.png"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    older_mtime = 1_700_000_000
    newer_mtime = older_mtime + 3600
    older.touch()
    newer.touch()
    import os

    os.utime(older, (older_mtime, older_mtime))
    os.utime(newer, (newer_mtime, newer_mtime))

    result = file_env.skill.execute("open latest screenshot", {"intent": "open_app"})

    assert result.success is True
    assert result.intent == "file_open"
    assert file_env.opened == [str(newer.resolve(strict=False))]
