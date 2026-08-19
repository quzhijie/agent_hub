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
                          agent_role
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
    finally:
        db._DB_PATH = None
