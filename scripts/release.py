"""Render the GitHub Release body for a version from ``CHANGELOG.md``.

The Docker publish workflow calls this after a successful image build, so the
release that shows up on the Releases page, the git tag and the ``:X.Y.Z``
image tag always describe the same commit.

Run it from the repository root::

    python3 scripts/release.py --print-version
    python3 scripts/release.py --version 2.9.4 --output release-notes.md
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

#: ``__version__ = "2.9.4"`` in ``utils/__init__.py`` — the single source of truth.
_VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

#: ``## v2.9.4`` — the section headings ``CHANGELOG.md`` uses.
_SECTION_LINE = re.compile(r"^##\s+v?(?P<version>\d+\.\d+\.\d+)\s*$")

DEFAULT_VERSION_FILE = "utils/__init__.py"
DEFAULT_CHANGELOG = "CHANGELOG.md"
DEFAULT_IMAGE = "ghcr.io/qcenjoyll/tg-dl"
DEFAULT_REPO = "QCEnjoyLL/tg-dl"
DEFAULT_PLATFORMS = "linux/amd64"


def read_version(version_file: str = DEFAULT_VERSION_FILE) -> str:
    """Return ``__version__`` from ``utils/__init__.py``."""
    source = Path(version_file).read_text(encoding="utf-8")
    match = _VERSION_LINE.search(source)
    if match is None:
        raise ValueError(f"{version_file} 里没有 __version__ 定义")
    return match.group(1)


def extract_section(changelog: str, version: str) -> str:
    """Return the body of the ``## v<version>`` section, without its heading."""
    lines = changelog.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _SECTION_LINE.match(line.strip())
        if match is not None and match.group("version") == version:
            start = index + 1
            break

    if start is None:
        raise ValueError(f"更新日志里没有 v{version} 的章节")

    body: List[str] = []
    for line in lines[start:]:
        if _SECTION_LINE.match(line.strip()):
            break
        body.append(line)

    section = "\n".join(body).strip()
    if not section:
        raise ValueError(f"v{version} 的章节是空的，无法生成发布说明")
    return section


def _format_platforms(platforms: str) -> str:
    """``linux/amd64, linux/arm64`` -> `` `linux/amd64`、`linux/arm64` ``."""
    items = [item.strip() for item in platforms.split(",") if item.strip()]
    if not items:
        items = [DEFAULT_PLATFORMS]
    return "、".join(f"`{item}`" for item in items)


def render_notes(
    changelog: str,
    version: str,
    image: str = DEFAULT_IMAGE,
    repo: str = DEFAULT_REPO,
    platforms: str = DEFAULT_PLATFORMS,
) -> str:
    """Build the full release body: changelog section + Docker instructions."""
    section = extract_section(changelog, version)
    major_minor = ".".join(version.split(".")[:2])
    return (
        f"{section}\n"
        "\n"
        "---\n"
        "\n"
        "### Docker 镜像\n"
        "\n"
        "```bash\n"
        f"docker pull {image}:{version}\n"
        "```\n"
        "\n"
        f"- `{image}:{version}` — 本版本（固定版本号，推荐）\n"
        f"- `{image}:{major_minor}` — 当前 minor 系列\n"
        f"- `{image}:latest` — 默认分支最新\n"
        f"- 架构：{_format_platforms(platforms)}\n"
        "\n"
        f"**完整更新日志**：[CHANGELOG.md](https://github.com/{repo}/blob/v{version}/"
        "CHANGELOG.md)\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub Release 正文")
    parser.add_argument(
        "--print-version", action="store_true", help="只打印 __version__ 后退出"
    )
    parser.add_argument("--version", help="要生成发布说明的版本号，例如 2.9.4")
    parser.add_argument("--version-file", default=DEFAULT_VERSION_FILE)
    parser.add_argument("--changelog", default=DEFAULT_CHANGELOG)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--platforms", default=DEFAULT_PLATFORMS)
    parser.add_argument("--output", help="写入文件而不是标准输出")
    args = parser.parse_args(argv)

    try:
        if args.print_version:
            print(read_version(args.version_file))
            return 0

        version = args.version or read_version(args.version_file)
        notes = render_notes(
            Path(args.changelog).read_text(encoding="utf-8"),
            version,
            image=args.image,
            repo=args.repo,
            platforms=args.platforms,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
