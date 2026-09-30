#!/usr/bin/env python3
"""Track tckdb-schemas / tckdb-client releases in TCKDB and keep the pins current.

Stdlib only (Python >= 3.11). The TCKDB repo, pinned commit and package
subdirectories live in ``tckdb-pin.toml`` (read by ``ci.yml`` at runtime); this
tool never edits a workflow file. Modes:

``--check``
    Read the adapter's pinned minor lines (schemas, client) and the pinned TCKDB
    commit, resolve the mirror's ``main`` (repo from ``tckdb-pin.toml``; ``--repo``
    and ``--ref`` override), read the two package versions at that commit, and print
    a JSON summary on stdout plus one human line on stderr. Exit 0 = no drift,
    10 = drift, anything else = error.

``--bump --sha SHA --schemas X.Y.Z --client A.B.C [--allow-downgrade]``
    Move the pins: the ``tckdb_arc/pyproject.toml`` dependency bounds, the
    ``TARGET_SCHEMAS_LINE`` in ``tckdb_arc/tests/_contract.py``, the ``sha`` in
    ``tckdb-pin.toml``, the install lines and "tested contract" sentence in
    ``README.md``, the version sentence and a changelog line in
    ``tckdb_arc/README.md``, and the adapter patch version. Idempotent: running it
    again with the same values changes nothing (the patch version and changelog
    line are added only when something else moved). Refuses to move a line
    backwards unless ``--allow-downgrade``.

``--since-output [--from X.Y.Z]``
    Run ``python -m tckdb_schemas.contract --since <from>`` with the installed
    tckdb_schemas and print it. The default ``--from`` is the pinned line of the
    current ``pyproject.toml``, so after a ``--bump`` pass the pre-bump line.

``--install-specs [--sha SHA]``
    Print ``TCKDB_CLIENT_SPEC=`` / ``TCKDB_SCHEMAS_SPEC=`` pip specifiers built from
    ``tckdb-pin.toml`` (for ``$GITHUB_ENV``).

Drift means an available package release line is *newer* than the pinned one; a
patch release inside the pinned line, or a new commit with the same versions, is
not drift (the pins already allow it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

DEFAULT_REF = "main"
EXIT_DRIFT = 10


# Paths of the files this tool reads and rewrites, relative to the repo root.
ADAPTER_PYPROJECT = "tckdb_arc/pyproject.toml"
PKG_README = "tckdb_arc/README.md"
CHANGELOG_MARKER = "<!-- tckdb-drift:changelog -->"
CONTRACT_PY = "tckdb_arc/tests/_contract.py"
PIN_FILE = "tckdb-pin.toml"
README = "README.md"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PIN_SHA = re.compile(r'^(sha\s*=\s*")[0-9a-f]{40}(")', re.M)

Line = tuple[int, int]


class DriftError(Exception):
    """A failure that is not drift (bad input, unreachable mirror, unparsable file)."""


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", text.strip(), re.ASCII)
    if not m:
        raise DriftError(f"not an X.Y.Z version: {text!r}")
    return int(m[1]), int(m[2]), int(m[3])


def line_of(version: str) -> Line:
    major, minor, _ = parse_version(version)
    return major, minor


def fmt_line(line: Line) -> str:
    return f"{line[0]}.{line[1]}"


def bound_for(line: Line) -> str:
    """``>=X.Y,<X.(Y+1)`` for a minor line."""
    return f">={line[0]}.{line[1]},<{line[0]}.{line[1] + 1}"


# ---------------------------------------------------------------------------
# Reading the adapter's pins
# ---------------------------------------------------------------------------


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_spec(pyproject_text: str, package: str) -> str:
    """The version specifier of ``package`` (extras and markers removed, spaces dropped)."""
    deps = tomllib.loads(pyproject_text)["project"]["dependencies"]
    for dep in deps:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)", dep)
        if m and _canonical(m[1]) == _canonical(package):
            return m[2].replace(" ", "")
    raise DriftError(f"{ADAPTER_PYPROJECT} has no {package} dependency")


def pinned_line(pyproject_text: str, package: str) -> Line:
    """The minor line a ``pkg>=X.Y,<X.(Y+1)`` dependency pins."""
    spec = _dependency_spec(pyproject_text, package)
    m = re.fullmatch(r">=([0-9]+)\.([0-9]+)(?:\.[0-9]+)?,<([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", spec, re.ASCII)
    if not m or (int(m[3]), int(m[4])) != (int(m[1]), int(m[2]) + 1):
        raise DriftError(f"{package} is not pinned to a single minor line: {package}{spec!r}")
    return int(m[1]), int(m[2])


def read_pin_file(text: str) -> dict:
    """The ``[tckdb]`` table of tckdb-pin.toml, validated."""
    try:
        table = tomllib.loads(text)["tckdb"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise DriftError(f"cannot read the [tckdb] table of {PIN_FILE}: {exc!r}") from exc
    for key in ("repo", "sha", "schemas_subdir", "client_subdir"):
        if not isinstance(table.get(key), str) or not table[key]:
            raise DriftError(f"{PIN_FILE} [tckdb] needs a non-empty string {key!r}")
    if not SHA_RE.match(table["sha"]):
        raise DriftError(f"{PIN_FILE} sha is not a 40-character lowercase hex commit")
    return table


def read_pins(root: Path) -> dict:
    pyproject = (root / ADAPTER_PYPROJECT).read_text()
    return {
        "schemas_line": pinned_line(pyproject, "tckdb-schemas"),
        "client_line": pinned_line(pyproject, "tckdb-client"),
        "sha": read_pin_file((root / PIN_FILE).read_text())["sha"],
    }


def adapter_version(pyproject_text: str) -> str:
    return tomllib.loads(pyproject_text)["project"]["version"]


# ---------------------------------------------------------------------------
# Reading the mirror
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise DriftError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_sha(repo: str, ref: str) -> str:
    """Resolve ``ref`` (branch, tag or full SHA) on ``repo`` to a commit SHA."""
    if SHA_RE.match(ref):
        return ref
    out = _git("ls-remote", repo, ref, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}")
    found = {}
    for row in out.splitlines():
        sha, _, name = row.partition("\t")
        found[name] = sha
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", ref):
        if name in found:
            return found[name]
    raise DriftError(f"{repo} has no ref {ref!r}")


def _fetch_by_git(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    """Shallow-fetch one commit into a temp repo and read ``paths`` from it.

    GitHub does not support ``git archive --remote``, so this fetches the single
    commit (blobless when the server allows it) rather than cloning history.
    """
    tmp = Path(tempfile.mkdtemp(prefix="tckdb-drift-"))
    try:
        _git("init", "-q", cwd=tmp)
        try:
            _git("fetch", "-q", "--depth", "1", "--filter=blob:none", repo, sha, cwd=tmp)
        except DriftError:
            _git("fetch", "-q", "--depth", "1", repo, sha, cwd=tmp)
        return {p: _git("show", f"FETCH_HEAD:{p}", cwd=tmp) for p in paths}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_by_raw_url(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?/?$", repo)
    if not m:
        raise DriftError(f"raw-URL fallback only supports github.com repos, got {repo!r}")
    owner, name = m[1], m[2]
    out = {}
    for p in paths:
        url = f"https://raw.githubusercontent.com/{owner}/{name}/{sha}/{p}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (https, fixed host)
                out[p] = resp.read().decode()
        except OSError as exc:
            raise DriftError(f"could not fetch {url}: {exc}") from exc
    return out


def fetch_files(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    try:
        return _fetch_by_git(repo, sha, paths)
    except DriftError as git_err:
        try:
            return _fetch_by_raw_url(repo, sha, paths)
        except DriftError as raw_err:
            raise DriftError(f"{git_err}; fallback: {raw_err}") from raw_err


def package_version(pyproject_text: str, path: str) -> str:
    try:
        version = tomllib.loads(pyproject_text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise DriftError(f"cannot read project.version from {path}: {exc!r}") from exc
    if not isinstance(version, str):
        raise DriftError(f"project.version in {path} is not a string")
    return "{}.{}.{}".format(*parse_version(version))


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def build_summary(pins: dict, repo: str, ref: str, sha: str, schemas_v: str, client_v: str) -> dict:
    def entry(pinned: Line, available: str) -> dict:
        avail_line = line_of(available)
        return {
            "pinned_line": fmt_line(pinned),
            "available_version": available,
            "available_line": fmt_line(avail_line),
            "newer": avail_line > pinned,
        }

    schemas = entry(pins["schemas_line"], schemas_v)
    client = entry(pins["client_line"], client_v)
    return {
        "repo": repo,
        "ref": ref,
        "pinned_sha": pins["sha"],
        "available_sha": sha,
        "available_sha7": sha[:7],
        "sha_changed": sha != pins["sha"],
        "schemas": schemas,
        "client": client,
        "drift": schemas["newer"] or client["newer"],
    }


def human_line(summary: dict) -> str:
    s, c = summary["schemas"], summary["client"]
    state = "DRIFT" if summary["drift"] else "no drift"
    return (
        f"{state}: tckdb-schemas {s['pinned_line']}.x -> {s['available_version']}, "
        f"tckdb-client {c['pinned_line']}.x -> {c['available_version']} "
        f"(TCKDB {summary['pinned_sha'][:7]} -> {summary['available_sha7']})"
    )


def run_check(root: Path, repo: str, ref: str) -> int:
    pins = read_pins(root)
    pin = read_pin_file((root / PIN_FILE).read_text())
    repo = repo or pin["repo"]
    sha = resolve_sha(repo, ref)
    schemas_path = f"{pin['schemas_subdir']}/pyproject.toml"
    client_path = f"{pin['client_subdir']}/pyproject.toml"
    files = fetch_files(repo, sha, [schemas_path, client_path])
    summary = build_summary(
        pins, repo, ref, sha,
        package_version(files[schemas_path], schemas_path),
        package_version(files[client_path], client_path),
    )
    print(json.dumps(summary, indent=2))
    print(human_line(summary), file=sys.stderr)
    return EXIT_DRIFT if summary["drift"] else 0


# ---------------------------------------------------------------------------
# --bump
# ---------------------------------------------------------------------------


def rewrite_pyproject(text: str, schemas_line: Line, client_line: Line) -> str:
    """Replace each package's version specifier, keeping its extras and marker."""
    for package, line in (("tckdb-schemas", schemas_line), ("tckdb-client", client_line)):
        text, n = re.subn(
            rf'"({re.escape(package)}(?:\s*\[[^\]"]*\])?)[^";]*?(\s*;[^"]*)?"',
            lambda m, line=line: f'"{m[1]}{bound_for(line)}{m[2] or ""}"',
            text, count=1,
        )
        if n != 1:
            raise DriftError(f'no "{package}..." dependency string in {ADAPTER_PYPROJECT}')
    return text


def bump_patch(text: str) -> str:
    """Increment the patch of the first top-level-looking ``version = "X.Y.Z"``."""
    def repl(m: re.Match) -> str:
        return f'{m[1]}{m[2]}.{m[3]}.{int(m[4]) + 1}{m[5]}'

    new, n = re.subn(r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', repl, text, count=1, flags=re.M)
    if n != 1:
        raise DriftError(f"no version = \"X.Y.Z\" line in {ADAPTER_PYPROJECT}")
    return new


def rewrite_contract_py(text: str, schemas_line: Line) -> str:
    new, n = re.subn(
        r"^(TARGET_SCHEMAS_LINE\s*=\s*)\(\d+,\s*\d+\)",
        lambda m: f"{m[1]}({schemas_line[0]}, {schemas_line[1]})",
        text, count=1, flags=re.M,
    )
    if n != 1:
        raise DriftError(f"no TARGET_SCHEMAS_LINE assignment in {CONTRACT_PY}")
    return new


def rewrite_pin_file(text: str, sha: str) -> str:
    """Set ``sha`` inside the ``[tckdb]`` table only (not a same-named key elsewhere)."""
    read_pin_file(text)
    header = re.search(r"^\[tckdb\][ \t]*(?:#.*)?$", text, re.M)
    if header is None:
        raise DriftError(f"no [tckdb] table header in {PIN_FILE}")
    nxt = re.search(r"^\[", text[header.end():], re.M)
    end = header.end() + nxt.start() if nxt else len(text)
    body, n = re.subn(r'^(sha\s*=\s*")[0-9a-f]{40}(")', lambda m: f"{m[1]}{sha}{m[2]}",
                      text[header.end():end], count=1, flags=re.M)
    if n != 1:
        raise DriftError(f'no sha = "..." line under [tckdb] in {PIN_FILE}')
    return text[:header.end()] + body + text[end:]


def rewrite_sha(text: str, repo: str, sha: str, where: str) -> str:
    """Replace the commit in ``<repo>@<sha>`` install lines (``repo`` from the pin file)."""
    new, n = re.subn(rf"({re.escape(repo)}@)[0-9a-f]{{40}}", lambda m: f"{m[1]}{sha}", text)
    if n == 0:
        raise DriftError(f"no pinned {repo}@<sha> install line to rewrite in {where}")
    return new


def rewrite_readme(text: str, repo: str, sha: str, schemas_line: Line, client_line: Line) -> str:
    text = rewrite_sha(text, repo, sha, README)
    new, n = re.subn(
        r"(`tckdb-client` )\d+\.\d+(\.x with `tckdb-schemas` )\d+\.\d+(\.x)",
        lambda m: f"{m[1]}{fmt_line(client_line)}{m[2]}{fmt_line(schemas_line)}{m[3]}",
        text, count=1,
    )
    if n != 1:
        raise DriftError(f"no 'tested contract' sentence to rewrite in {README}")
    return new


def rewrite_pkg_readme(text: str, schemas_line: Line, client_line: Line) -> str:
    new, n = re.subn(
        r"(`tckdb-client` )\d+\.\d+(\.x and `tckdb-schemas` )\d+\.\d+(\.x)",
        lambda m: f"{m[1]}{fmt_line(client_line)}{m[2]}{fmt_line(schemas_line)}{m[3]}",
        text, count=1,
    )
    if n != 1:
        raise DriftError(f"no version sentence to rewrite in {PKG_README}")
    return new


def add_changelog_line(text: str, line: str) -> str:
    if CHANGELOG_MARKER not in text:
        raise DriftError(f"no {CHANGELOG_MARKER} marker in {PKG_README}")
    if line in text:
        return text
    return text.replace(CHANGELOG_MARKER, f"{CHANGELOG_MARKER}\n{line}", 1)


def run_bump(root: Path, sha: str, schemas: str, client: str, allow_downgrade: bool = False) -> int:
    if not SHA_RE.match(sha):
        raise DriftError(f"--sha must be a full 40-character lowercase hex commit, got {sha!r}")
    schemas, client = "{}.{}.{}".format(*parse_version(schemas)), "{}.{}.{}".format(*parse_version(client))
    schemas_line, client_line = line_of(schemas), line_of(client)

    def read(rel: str) -> str:
        return (root / rel).read_text()

    old = {rel: read(rel) for rel in (ADAPTER_PYPROJECT, CONTRACT_PY, PIN_FILE, README, PKG_README)}
    if not allow_downgrade:
        for package, new_line in (("tckdb-schemas", schemas_line), ("tckdb-client", client_line)):
            current = pinned_line(old[ADAPTER_PYPROJECT], package)
            if new_line < current:
                raise DriftError(
                    f"refusing to move {package} back from {fmt_line(current)} to {fmt_line(new_line)}; "
                    "pass --allow-downgrade if that is intended"
                )
    repo = read_pin_file(old[PIN_FILE])["repo"]
    pyproject = rewrite_pyproject(old[ADAPTER_PYPROJECT], schemas_line, client_line)
    new = {
        CONTRACT_PY: rewrite_contract_py(old[CONTRACT_PY], schemas_line),
        PIN_FILE: rewrite_pin_file(old[PIN_FILE], sha),
        README: rewrite_readme(old[README], repo, sha, schemas_line, client_line),
        PKG_README: rewrite_pkg_readme(old[PKG_README], schemas_line, client_line),
    }
    others_changed = any(new[rel] != old[rel] for rel in new) or pyproject != old[ADAPTER_PYPROJECT]
    # Bump the patch (and log it) only when something else moved, so a repeated run is a no-op.
    if others_changed:
        new[ADAPTER_PYPROJECT] = bump_patch(pyproject)
        version = adapter_version(new[ADAPTER_PYPROJECT])
        new[PKG_README] = add_changelog_line(
            new[PKG_README],
            f"- Adapter {version}: tracked TCKDB {sha[:7]} (schemas {schemas}, client {client}).",
        )
    else:
        new[ADAPTER_PYPROJECT] = pyproject

    changed = [rel for rel in new if new[rel] != old[rel]]
    for rel in changed:
        (root / rel).write_text(new[rel])
    if not changed:
        print("already up to date; nothing changed")
        return 0
    print(
        f"schemas -> {bound_for(schemas_line)} (target {fmt_line(schemas_line)}), "
        f"client -> {bound_for(client_line)}, TCKDB {sha[:7]}, adapter "
        f"{adapter_version(old[ADAPTER_PYPROJECT])} -> {adapter_version(new[ADAPTER_PYPROJECT])}"
    )
    for rel in changed:
        print(f"  changed {rel}")
    return 0


def run_install_specs(root: Path, sha: str | None) -> int:
    pin = read_pin_file((root / PIN_FILE).read_text())
    sha = sha or pin["sha"]
    if not SHA_RE.match(sha):
        raise DriftError(f"--sha must be a full 40-character lowercase hex commit, got {sha!r}")
    base = f"git+{pin['repo']}@{sha}#subdirectory="
    print(f"TCKDB_CLIENT_SPEC=tckdb-client @ {base}{pin['client_subdir']}")
    print(f"TCKDB_SCHEMAS_SPEC=tckdb-schemas @ {base}{pin['schemas_subdir']}")
    return 0


# ---------------------------------------------------------------------------
# --since-output
# ---------------------------------------------------------------------------


def run_since_output(root: Path, since: str | None) -> int:
    if since is None:
        line = pinned_line((root / ADAPTER_PYPROJECT).read_text(), "tckdb-schemas")
        since = f"{fmt_line(line)}.0"
    parse_version(since)
    proc = subprocess.run(
        [sys.executable, "-m", "tckdb_schemas.contract", "--since", since],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
        raise DriftError(f"tckdb_schemas.contract --since {since} exited {proc.returncode}")
    print(proc.stdout, end="")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift vs the mirror (exit 10 on drift)")
    mode.add_argument("--bump", action="store_true", help="rewrite the pins (needs --sha --schemas --client)")
    mode.add_argument("--since-output", action="store_true", help="print tckdb_schemas.contract --since <pin>")
    mode.add_argument("--install-specs", action="store_true", help="print pip specifiers from tckdb-pin.toml")
    p.add_argument("--repo", default=None, help="TCKDB mirror, overriding the repo in tckdb-pin.toml")
    p.add_argument("--ref", default=DEFAULT_REF, help="branch, tag or SHA to resolve (default: %(default)s)")
    p.add_argument("--sha", help="--bump: full commit SHA to pin")
    p.add_argument("--schemas", help="--bump: available tckdb-schemas version X.Y.Z")
    p.add_argument("--client", help="--bump: available tckdb-client version A.B.C")
    p.add_argument("--allow-downgrade", action="store_true", help="--bump: allow moving a line backwards")
    p.add_argument("--from", dest="since", help="--since-output: base version (default: pinned line .0)")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    try:
        if args.check:
            return run_check(args.root, args.repo, args.ref)
        if args.bump:
            if not (args.sha and args.schemas and args.client):
                p.error("--bump needs --sha, --schemas and --client")
            return run_bump(args.root, args.sha, args.schemas, args.client, args.allow_downgrade)
        if args.install_specs:
            return run_install_specs(args.root, args.sha)
        return run_since_output(args.root, args.since)
    except DriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
