import json
import sqlite3


def test_existing_projects_and_sessions_default_to_unbound(tmp_path):
    from app import db

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_dir TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_removed INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            provider TEXT NOT NULL,
            launch_command TEXT NOT NULL DEFAULT '',
            working_dir TEXT NOT NULL,
            tmux_session TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'unknown',
            last_output TEXT NOT NULL DEFAULT '',
            last_activity_at TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            removed_at TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO projects
            (id, name, root_dir, created_at, updated_at)
        VALUES ('project-old', 'Old', '/tmp/old', '2026-01-01', '2026-01-01');
        INSERT INTO sessions
            (id, project_id, name, provider, working_dir, tmux_session, created_at)
        VALUES
            ('seat-old', 'project-old', 'Seat', 'codex', '/tmp/old',
             'agent_hub_old', '2026-01-01');
        """
    )
    connection.close()

    try:
        db.init_db(path)
        with db.connect() as migrated:
            project = migrated.execute(
                """SELECT project_core_tracking, project_core_project_id,
                          project_core_project_title
                   FROM projects WHERE id='project-old'"""
            ).fetchone()
            session = migrated.execute(
                """SELECT project_core_tracking, project_core_lifecycle,
                          project_core_report_warning, initial_prompt, project_core_json,
                          agent_role, resume_prompt_pending, model, permission_mode,
                          provider_session_id
                   FROM sessions WHERE id='seat-old'"""
            ).fetchone()
        assert project["project_core_tracking"] == "off"
        assert project["project_core_project_id"] == ""
        assert project["project_core_project_title"] == ""
        assert session["project_core_tracking"] == "off"
        assert session["project_core_lifecycle"] == "untracked"
        assert session["project_core_report_warning"] == ""
        assert session["initial_prompt"] == ""
        assert session["project_core_json"] == "{}"
        assert session["agent_role"] == "general"
        assert session["resume_prompt_pending"] == 0
        assert session["model"] == ""
        assert session["permission_mode"] == "default"
        assert session["provider_session_id"] == ""
    finally:
        db._DB_PATH = None


def test_generated_project_core_seat_names_shorten_without_overwriting_user_names(
    tmp_path,
):
    from app import db

    path = tmp_path / "seat-names.db"
    try:
        db.init_db(path)
        metadata = json.dumps({
            "registration_status": "registered",
            "project_title": "Project Core",
            "workstream_title": "Agent change channel",
            "record_id": "rec_agent_change",
        })
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO projects(
                       id,name,root_dir,created_at,updated_at
                   ) VALUES ('project','Project Core','/tmp/project','now','now')"""
            )
            for seat_id, name in (
                ("generated", "PC · Project Core › Agent change channel"),
                ("custom", "My focused review seat"),
                ("custom-shaped", "PC · user-authored note › Agent change channel"),
            ):
                connection.execute(
                    """INSERT INTO sessions(
                           id,project_id,name,provider,working_dir,tmux_session,
                           created_at,project_core_json
                       ) VALUES (?,?,?,'codex','/tmp/project',?, 'now',?)""",
                    (seat_id, "project", name, f"tmux-{seat_id}", metadata),
                )

        db.init_db(path)

        with db.connect() as connection:
            names = {
                row["id"]: row["name"]
                for row in connection.execute(
                    "SELECT id,name FROM sessions ORDER BY id"
                ).fetchall()
            }
        assert names == {
            "custom": "My focused review seat",
            "custom-shaped": "PC · user-authored note › Agent change channel",
            "generated": "Agent change channel",
        }
    finally:
        db._DB_PATH = None


def test_seat_name_migration_ignores_non_object_project_core_json(tmp_path):
    from app import db

    path = tmp_path / "non-object-project-core.db"
    try:
        db.init_db(path)
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO projects(
                       id,name,root_dir,created_at,updated_at
                   ) VALUES ('project','Project','/tmp/project','now','now')"""
            )
            for index, metadata in enumerate(("[]", "null", '"metadata"')):
                connection.execute(
                    """INSERT INTO sessions(
                           id,project_id,name,provider,working_dir,tmux_session,
                           created_at,project_core_json
                       ) VALUES (?, 'project', 'seat', 'codex', '/tmp/project', ?,
                                 'now', ?)""",
                    (f"seat-{index}", f"tmux-{index}", metadata),
                )

        db.init_db(path)

        with db.connect() as connection:
            self_names = connection.execute(
                "SELECT name FROM sessions ORDER BY id"
            ).fetchall()
        assert [row["name"] for row in self_names] == ["seat", "seat", "seat"]
    finally:
        db._DB_PATH = None
