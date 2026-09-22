import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools" / "build_windows_portable.ps1"
LOCK = ROOT / "tools" / "windows_portable_requirements.lock"
WEB_REQUIREMENTS = ROOT / "web_requirements.txt"
WEB_INSTALLER = ROOT / "install_web.bat"
SQLITE_AUDITOR = ROOT / "tools" / "audit_release_sqlite.py"


def test_portable_builder_is_ascii_for_windows_powershell_51():
    # Windows PowerShell 5.1 treats UTF-8 without a BOM as the legacy codepage.
    assert BUILDER.read_bytes().isascii()


def test_portable_builder_uses_sanitized_committed_source_and_verified_components():
    script = BUILDER.read_text(encoding="utf-8")
    tracked_inputs = script.split("$trackedBuildInputs = @(", 1)[1].split("\n)", 1)[0]

    assert "build_github_package.ps1" in script
    assert "git -C $sourceRoot diff --quiet HEAD" in script
    assert "Portable build inputs must be committed" in script
    # A user's live glossary may be dirty; staging always reads the sanitized
    # committed copy, so personal data must neither leak nor block packaging.
    assert '"data"' not in tracked_inputs
    assert "Get-VerifiedDownload" in script
    assert "audit_release_sqlite.py" in script
    assert "--require-hashes" in script
    assert "python-$pythonVersion-embed-amd64.zip" in script
    assert "deno-x86_64-pc-windows-msvc.zip" in script
    assert re.search(r'\$pythonSha256 = "[0-9a-f]{64}"', script)
    assert re.search(r'\$denoSha256 = "[0-9a-f]{64}"', script)


def test_portable_core_excludes_private_state_and_large_optional_runtimes():
    script = BUILDER.read_text(encoding="utf-8")
    lock = LOCK.read_text(encoding="utf-8")

    for forbidden in (
        ".env",
        "youtube_cookies.txt",
        "job.json",
        "translation_memory.json",
        "download|reports|tests|tools|\\.git",
    ):
        assert forbidden in script
    assert "ffmpeg_bundled = $false" in script
    assert "whisper_bundled = $false" in script
    assert "FFmpeg must not enter the Core portable package" in script
    assert "mutagen==" not in lock
    assert "faster-whisper" not in lock
    assert "pyinstaller" not in lock.casefold()


def test_developer_directory_gate_only_rejects_release_root_directories():
    script = BUILDER.read_text(encoding="utf-8")

    assert '"(?i)^(download|reports|tests|tools|\\.git)([\\\\/]|$)"' in script


def test_portable_launcher_is_isolated_and_keeps_secrets_out_of_scripts():
    script = BUILDER.read_text(encoding="utf-8")

    assert '"%~dp0runtime\\python\\python.exe" -I -m web.launcher' in script
    assert 'set "SUBTITLE_PORTABLE=1"' in script
    assert 'set "SUBTITLE_DISTRIBUTION=windows-x64-portable-core"' in script
    assert 'set "PYTHONPATH="' in script
    assert "LLM_API_KEY=" not in script
    assert "youtube_cookies" not in script.split("$startScript = @'", 1)[1].split("'@", 1)[0]


def test_sqlite_release_auditor_never_prints_matched_values():
    script = SQLITE_AUDITOR.read_text(encoding="utf-8")

    assert "mode=ro" in script
    assert '"locations"' in script
    assert '"hit_count"' in script
    assert "candidate" not in script.split("return {", 1)[1]


def test_portable_dependency_lock_is_exact_and_complete():
    lock = LOCK.read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock, re.MULTILINE))

    assert pinned["fastapi"]
    assert pinned["httpx"]
    assert pinned["python-multipart"]
    assert pinned["uvicorn"]
    assert pinned["yt-dlp"]
    assert pinned["yt-dlp-ejs"]
    assert pinned["pycryptodomex"]
    assert all(re.fullmatch(r"\d+(?:\.\d+)+(?:[A-Za-z0-9.-]+)?", version)
               for version in pinned.values())
    assert "--hash=sha256:" in lock


def test_source_web_installer_uses_the_hash_locked_binary_runtime():
    requirements = WEB_REQUIREMENTS.read_text(encoding="utf-8")
    installer = WEB_INSTALLER.read_text(encoding="utf-8")

    assert "tools/windows_portable_requirements.lock" in requirements
    assert ">=" not in requirements
    assert "--require-hashes" in installer
    assert "--only-binary=:all:" in installer
    assert "-U yt-dlp" not in installer


def test_runtime_component_manifest_shape_example_is_non_secret():
    """Document the public-only fields emitted by the PowerShell builder."""
    component = {
        "package": "subtitle-pipeline-windows-x64-portable-core",
        "version": "0.2.0-beta.3",
        "python": {"version": "3.13.15", "url": "https://www.python.org/", "sha256": "0" * 64},
        "deno": {"version": "2.9.5", "url": "https://github.com/denoland/deno", "sha256": "1" * 64},
        "ffmpeg_bundled": False,
        "whisper_bundled": False,
        "python_packages": [],
    }
    encoded = json.dumps(component)

    assert "api_key" not in encoded.casefold()
    assert "cookie" not in encoded.casefold()
    assert "userprofile" not in encoded.casefold()
