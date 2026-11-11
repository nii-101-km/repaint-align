import pytest

import audacity_adapter as adapter_module
from audacity_adapter import MACRO_NAME, NOOP, OWNER, install_macro


@pytest.mark.parametrize("name", ["test", "Тестовый проект — стерео", "name\nwith newline", ""])
def test_project_metadata_preserves_unicode_and_newlines(name):
    assert adapter_module.project_metadata(name + "\n1\n48000") == (name, 1, 48000)


def test_macro_install_never_overwrites_an_existing_user_macro(tmp_path):
    path = tmp_path / (MACRO_NAME + ".txt")
    path.write_text("A user macro that must remain intact\n")
    with pytest.raises(ValueError, match="another macro"):
        install_macro(tmp_path)
    assert path.read_text() == "A user macro that must remain intact\n"


def test_macro_install_is_safe_to_repeat_and_disarms_old_commands(tmp_path):
    path = install_macro(tmp_path)
    assert path.read_text() == NOOP
    path.write_text("SetClip: At=1 Start=2\n")
    assert install_macro(tmp_path) == path
    assert path.read_text() == NOOP
    assert (tmp_path / ("." + MACRO_NAME + ".owner")).read_text() == OWNER


def test_macro_install_rejects_symlink(tmp_path):
    original = tmp_path / "other.txt"
    original.write_text("preserve")
    (tmp_path / (MACRO_NAME + ".txt")).symlink_to(original)
    with pytest.raises(ValueError, match="symbolic link"):
        install_macro(tmp_path)
    assert original.read_text() == "preserve"


@pytest.mark.parametrize("fails", [False, True])
def test_metadata_query_prepares_audio_range_and_restores_undo_cursor(monkeypatch, fails):
    class Pipe:
        w = 1

        def info(self, kind):
            return (
                [{"selected": True, "kind": "wave"}]
                if kind == "Tracks"
                else [{"track": 0, "start": 2.0, "end": 3.2}]
            )

    times = []
    monkeypatch.setattr(adapter_module, "precise_selection", lambda pipe: (3.2, 3.2))
    monkeypatch.setattr(
        adapter_module, "set_time", lambda pipe, start, end: times.append((start, end))
    )
    try:
        with adapter_module.Audacity()._metadata_selection(Pipe()):
            assert 2 < times[0][0] < times[0][1] < 3.2
            if fails:
                raise RuntimeError("query failed")
    except RuntimeError:
        assert fails
    assert times[-1] == (3.2, 3.2)


def test_metadata_query_rejects_no_audio_before_calling_nyquist():
    class Pipe:
        def info(self, kind):
            return [{"selected": True, "kind": "label"}]

    with pytest.raises(ValueError, match="audio track"):
        with adapter_module.Audacity()._metadata_selection(Pipe()):
            pytest.fail("must not run Nyquist without an audio track")


def test_atomic_macro_replacement_is_owner_only_and_cleans_up(tmp_path):
    import stat

    path = tmp_path / "helper.txt"
    adapter_module.atomic_text(path, "first")
    adapter_module.atomic_text(path, "second")
    assert path.read_text() == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_macro_failed_replace_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "helper.txt"
    path.write_text("original")

    def fail(*args):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(adapter_module.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        adapter_module.atomic_text(path, "new")
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]
