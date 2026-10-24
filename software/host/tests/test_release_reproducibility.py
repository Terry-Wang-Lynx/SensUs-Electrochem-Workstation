from pathlib import Path


ROOT = Path(__file__).parents[3]


def test_manual_release_assets_have_one_publisher():
    configuration = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/portable-release.yml").read_text(encoding="utf-8")
    assert '[tool.sensus.release]' in configuration
    assert 'publisher = "manual"' in configuration
    assert 'channel = "preview"' in configuration
    assert "needs.release-metadata.outputs.publisher == 'ci'" in workflow


def test_portable_analysis_uses_the_same_psd_backend_as_source():
    configuration = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    portable = configuration.split("portable = [", 1)[1].split("]", 1)[0]
    assert '"scipy>=1.10"' in portable
    for name in ("portable-macos.lock", "portable-windows.lock"):
        assert "scipy==" in (ROOT / "packaging" / name).read_text(encoding="utf-8")
    assert '"scipy.signal"' in (ROOT / "packaging/portable.spec").read_text(encoding="utf-8")


def test_source_verification_dependencies_are_locked_with_hashes():
    lock = ROOT / "packaging/source-test.lock"
    assert lock.is_file()
    content = lock.read_text(encoding="utf-8")
    assert "pytest==" in content and "scipy==" in content
    assert "--hash=sha256:" in content
