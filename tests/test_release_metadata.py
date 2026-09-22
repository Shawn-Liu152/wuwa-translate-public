from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent_across_user_facing_files():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert f"当前版本：{version}" in readme
    assert f"## [{version}]" in changelog
    assert f'id="app-version" translate="no">v{version}</small>' in index


def test_release_builder_rejects_local_cookie_file():
    builder = (ROOT / "tools" / "build_github_package.ps1").read_text(
        encoding="utf-8"
    )

    assert '"youtube_cookies.txt"' in builder


def test_release_builder_packages_committed_head_not_dirty_directories():
    builder = (ROOT / "tools" / "build_github_package.ps1").read_text(
        encoding="utf-8"
    )

    assert "git" in builder
    assert "archive" in builder
    assert "HEAD" in builder
    assert "Expand-Archive" in builder
    assert '"reports"' not in builder
    assert '"AGENTS.md"' not in builder
    assert '"docs\\HANDOFF_NEXT_WINDOW.md"' in builder
    assert "Get-ChildItem -LiteralPath $sourceDirectory -Recurse -File" not in builder
    assert '$relativePackagePath -match "(^|[\\\\/])download' in builder
    assert "pre-language-migration" in builder


def test_release_builder_allowlists_only_the_public_demo_asset_from_docs():
    builder = (ROOT / "tools" / "build_github_package.ps1").read_text(
        encoding="utf-8"
    )
    directory_block = builder.split("$directories = @(", 1)[1].split(")", 1)[0]

    assert '"docs"' not in directory_block
    assert '"docs\\assets\\demo-workbench.png"' in builder
    assert '$relativePackagePath -match "(^|[\\\\/])docs([\\\\/]|$)"' in builder
    assert '$relativePackagePath -ne "docs\\assets\\demo-workbench.png"' in builder


def test_release_builder_has_identity_and_secret_privacy_gates():
    builder = (ROOT / "tools" / "build_github_package.ps1").read_text(
        encoding="utf-8"
    )

    assert "$privacyPatterns" in builder
    assert "$textExtensions" in builder
    assert "Personal path or email found" in builder
    assert "[Environment]::UserName" in builder
    assert "Local account marker found" in builder
    assert "PRIVATE KEY" in builder


def test_release_builder_creates_a_labeled_beta_zip_and_checksum():
    builder = (ROOT / "tools" / "build_github_package.ps1").read_text(
        encoding="utf-8"
    )

    assert "windows-beta-source.zip" in builder
    assert "Compress-Archive" in builder
    assert "Get-FileHash" in builder
    assert "SHA256" in builder
