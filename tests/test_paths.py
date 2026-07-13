import pytest

from app.paths import validate_dir


def test_accepts_existing_absolute_dir(tmp_path):
    assert validate_dir(str(tmp_path)) == str(tmp_path)


def test_rejects_relative(tmp_path):
    with pytest.raises(ValueError):
        validate_dir("relative/path")


def test_rejects_missing(tmp_path):
    with pytest.raises(ValueError):
        validate_dir(str(tmp_path / "does-not-exist"))


def test_rejects_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ValueError):
        validate_dir(str(f))


def test_rejects_empty():
    with pytest.raises(ValueError):
        validate_dir("   ")
