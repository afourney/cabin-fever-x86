import json

from cabin_fever_x86 import _updates


def test_a_fresh_cached_version_avoids_pypi(tmp_path, monkeypatch):
    (tmp_path / _updates.CACHE_NAME).write_text(
        json.dumps({"checked_at": 100, "version": "1.2.3"}), encoding="utf-8"
    )

    monkeypatch.setattr(_updates, "_pypi_version", lambda: (_ for _ in ()).throw(AssertionError))

    assert _updates.latest_version(tmp_path, now=101) == "1.2.3"


def test_an_expired_cache_is_refreshed(tmp_path, monkeypatch):
    cache = tmp_path / _updates.CACHE_NAME
    cache.write_text(json.dumps({"checked_at": 100, "version": "1.2.3"}), encoding="utf-8")
    monkeypatch.setattr(_updates, "_pypi_version", lambda: "1.3.0")

    now = 100 + _updates.CACHE_MAX_AGE
    assert _updates.latest_version(tmp_path, now=now) == "1.3.0"
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "checked_at": now,
        "version": "1.3.0",
    }


def test_pip_upgrade_guidance_is_one_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_updates, "latest_version", lambda home: "1.1.0")
    monkeypatch.setattr(_updates, "_installer_name", lambda: "pip")

    _updates.print_upgrade_notice(tmp_path, "1.0.0")

    output = capsys.readouterr().out
    assert "A newer launcher is available: 1.1.0" in output
    assert "python -m pip install --upgrade cabin-fever-x86" in output
    assert "uv tool" not in output


def test_uv_upgrade_guidance_has_only_uv_options(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_updates, "latest_version", lambda home: "1.1.0")
    monkeypatch.setattr(_updates, "_installer_name", lambda: "uv")

    _updates.print_upgrade_notice(tmp_path, "1.0.0")

    output = capsys.readouterr().out
    assert "uv tool upgrade cabin-fever-x86" in output
    assert "uv pip install --upgrade cabin-fever-x86" in output
    assert "python -m pip" not in output


def test_no_notice_for_current_or_source_tree_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_updates, "latest_version", lambda home: "1.0.0")

    _updates.print_upgrade_notice(tmp_path, "1.0.0")
    _updates.print_upgrade_notice(tmp_path, None)

    assert capsys.readouterr().out == ""
