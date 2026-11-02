"""Strict JSONC and exact Git materials. No LFS/submodule/symlink support.

Callers allowlist repositories and enforce that only root-base FROM scratch
recipes omit base_digest. Injected transport is exclusively for offline tests.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def _fail(message):
    raise ConfigError(message)


def parse_jsonc(text: str):
    if not isinstance(text, str):
        _fail("JSONC input must be text")
    # Lexing preserves quoted URLs, escaped quotes and comment-like strings.
    output, i, quoted, escape = [], 0, False, False
    while i < len(text):
        char = text[i]
        if quoted:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quoted = False
            i += 1
        elif char == '"':
            quoted = True
            output.append(char)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            output.extend(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                _fail("Unterminated block comment")
            output.extend("\n" if c == "\n" else " " for c in text[i:end + 2])
            i = end + 2
        else:
            output.append(char)
            i += 1
    stripped = "".join(output)
    output, quoted, escape = [], False, False
    for i, char in enumerate(stripped):
        if quoted:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            output.append(char)
        elif char == ",":
            j = i + 1
            while j < len(stripped) and stripped[j] in " \t\r\n":
                j += 1
            previous = stripped[:i].rstrip()[-1:]
            trailing = j < len(stripped) and stripped[j] in "}]" and previous not in "[{,:"
            output.append(" " if trailing else char)
        else:
            output.append(char)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            _fail("Nonfinite number")
        return result

    try:
        return json.loads("".join(output), object_pairs_hook=pairs,
                          parse_constant=lambda _: _fail("Nonfinite number"), parse_float=number)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ConfigError(f"Invalid JSONC: {exc}") from exc


def _object(value, allowed, required, label):
    if type(value) is not dict:
        _fail(f"{label} must be an object")
    if set(value) - set(allowed) or set(required) - set(value):
        _fail(f"{label} has missing or unknown fields")


def _text(value, label):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        _fail(f"{label} must be nonempty text without control characters")
    return value


def _sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        _fail(f"{label} must be a full lowercase 40-hex Git SHA")
    return value


def _repository(value):
    _text(value, "repository")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", parsed.path)):
        _fail("repository must be a credential-free https://github.com/owner/repo URL")
    result = value.removesuffix(".git")
    if result.rsplit("/", 1)[-1] in (".", ".."):
        _fail("Invalid repository name")
    return result


def _ref(value, label):
    _text(value, label)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
            or ".." in value or "//" in value or value.endswith(("/", "."))
            or any(p.startswith(".") or p.endswith(".lock") for p in value.split("/"))):
        _fail(f"Invalid {label}")
    return value


def _subdirectory(value):
    _text(value, "subdirectory")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("..", ".git") for p in path.parts) or "\\" in value or str(path) != value:
        _fail("subdirectory must be a normalized relative path")
    return value


def validate_config(obj, ci: bool) -> dict:
    _object(obj, {"schemaVersion", "dependencies", "build"},
            {"schemaVersion", "dependencies", "build"}, "config")
    if type(obj["schemaVersion"]) is not int or obj["schemaVersion"] != 1:
        _fail("schemaVersion must be 1")
    if type(obj["dependencies"]) is not dict:
        _fail("dependencies must be an object")
    result = copy.deepcopy(obj)
    for name, dep in result["dependencies"].items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
            _fail("Invalid dependency name")
        _object(dep, {"repository", "branch", "commit", "version_tag", "subdirectory", "local-path"},
                {"repository", "branch", "commit"}, f"dependency {name}")
        dep["repository"] = _repository(dep["repository"])
        _ref(dep["branch"], "branch")
        _sha(dep["commit"], "commit")
        if "version_tag" in dep:
            _ref(dep["version_tag"], "version_tag")
            if not re.fullmatch(r"(?:fixture/[A-Za-z0-9_-]+/)?v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", dep["version_tag"]):
                _fail("version_tag must be vX.Y.Z or fixture/name/vX.Y.Z")
        dep["subdirectory"] = _subdirectory(dep.get("subdirectory", "."))
        if "local-path" in dep:
            if ci:
                _fail("CI forbids local-path, including empty values")
            local = _text(dep["local-path"], "local-path")
            if ".." in Path(local).parts or "\\" in local:
                _fail("local-path must not contain traversal")
    build = result["build"]
    _object(build, {"platform", "variant", "args"}, {"platform"}, "build")
    if build["platform"] != "linux/amd64" or build.get("variant", "standard") != "standard":
        _fail("Only linux/amd64 and the standard variant are supported")
    build.setdefault("variant", "standard")
    build.setdefault("args", {})
    _object(build["args"], {"MODE"}, set(), "build.args")
    build["args"].setdefault("MODE", "release")
    mode = _text(build["args"]["MODE"], "MODE")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", mode):
        _fail("MODE must be a simple value of at most 64 characters")
    return result


def load_config(ci: bool, snapshot_text: str | None, local_file: Path | None,
                example_file: Path) -> dict:
    if ci:
        if snapshot_text is None:
            _fail("CI requires an explicit config snapshot; there is no fallback")
        return validate_config(parse_jsonc(snapshot_text), True)
    source = local_file if local_file is not None and local_file.exists() else example_file
    return validate_config(parse_jsonc(source.read_text()), False)


def _safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink():
            _fail(f"Symlink path is unsupported: {item}")
    return path


def _git(path, *args, file_transport=False):
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "protocol.file.allow=" + ("always" if file_transport else "never"),
               "-C", str(path), *args]
    run = subprocess.run(command, env=env, text=True, capture_output=True, timeout=120)
    if run.returncode:
        raise ConfigError(f"Git {args[0]} failed: {run.stderr.strip()}")
    return run.stdout.strip()


def _material_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in dirs + sorted(files):
            path = Path(current) / name
            if name == ".git":
                continue
            if path.is_symlink():
                _fail("Symlink materials are unsupported")
            if path.is_dir():
                label = path.relative_to(directory).as_posix().encode()
                digest.update(b"directory:" + len(label).to_bytes(8, "big") + label
                              + (path.stat().st_mode & 0o777).to_bytes(2, "big"))
                continue
            if not path.is_file():
                _fail("Special-file materials are unsupported")
            if name == ".gitmodules":
                _fail("Submodule materials are unsupported")
            data = path.read_bytes()
            if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
                _fail("Git LFS materials are unsupported")
            if name == ".gitattributes" and re.search(rb"filter\s*=\s*lfs", data):
                _fail("Git LFS attributes are unsupported")
            label = path.relative_to(directory).as_posix().encode()
            mode = (path.stat().st_mode & 0o777).to_bytes(2, "big")
            digest.update(len(label).to_bytes(8, "big") + label + mode)
            digest.update(len(data).to_bytes(8, "big") + data)
    return "sha256:" + digest.hexdigest()


def _version(path, dep):
    selected = dep.get("version_tag")
    if selected is None:
        tags = _git(path, "tag", "--points-at", dep["commit"]).splitlines()
        tags = [t for t in tags if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", t)]
        if len(tags) != 1:
            _fail("Dependency requires exactly one global version tag or an explicit version_tag")
        selected = tags[0]
    target = _git(path, "rev-parse", "--verify", f"refs/tags/{selected}^{{commit}}")
    if target != dep["commit"]:
        _fail("Dependency version_tag does not target the pinned commit")
    return selected, selected.rsplit("/", 1)[-1].removeprefix("v")


def resolve_dependencies(config, root: Path, allowed_repositories: set[str], ci: bool,
                         *, transport: dict[str, str] | None = None) -> list[dict]:
    config = validate_config(config, ci)
    root = _safe_path(root)
    allowlist = {_repository(r) for r in allowed_repositories}
    records = []
    for name, dep in sorted(config["dependencies"].items()):
        repository, commit = dep["repository"], dep["commit"]
        if repository not in allowlist:
            _fail(f"Repository not allowlisted: {repository}")
        local = "local-path" in dep
        branch_tip = None
        if local:
            path = _safe_path(root / dep["local-path"])
            if not path.is_dir():
                _fail("local-path must be an existing directory")
            _safe_path(path / ".git")
            # Declared pin/tag must exist even when local working bytes differ.
            version_tag, version = _version(path, dep)
            branch_tip = _git(path, "rev-parse", "--verify", f"refs/heads/{dep['branch']}^{{commit}}")
            _git(path, "merge-base", "--is-ancestor", commit, branch_tip)
        else:
            repo_key = repository.rsplit("/", 1)[-1] + "-" + hashlib.sha256(repository.encode()).hexdigest()[:12]
            path = _safe_path(root / ".local" / repo_key / commit)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                # Publish a complete cache atomically. Failed staging directories
                # remain available for diagnosis and do not poison future retry.
                staging = Path(tempfile.mkdtemp(prefix=".fetch-" + commit + "-", dir=path.parent))
                _git(staging, "init", "--quiet")
                url = (transport or {}).get(repository, repository)
                _git(staging, "remote", "add", "origin", url)
                _git(staging, "fetch", "--no-tags", "origin",
                     f"+refs/heads/{dep['branch']}:refs/remotes/origin/{dep['branch']}",
                     "+refs/tags/*:refs/tags/*", file_transport=transport is not None)
                _git(staging, "cat-file", "-e", f"{commit}^{{commit}}")
                _git(staging, "merge-base", "--is-ancestor", commit, f"refs/remotes/origin/{dep['branch']}")
                _git(staging, "checkout", "--quiet", "--detach", commit)
                try:
                    staging.rename(path)
                except OSError:
                    if not path.is_dir():
                        raise
                    # Another resolver won the race; validate its cache below.
            _safe_path(path / ".git")
            if _git(path, "rev-parse", "HEAD") != commit:
                _fail("Cached dependency HEAD is not the exact pin")
            if _git(path, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
                _fail("Cached dependency must be detached")
            if _git(path, "status", "--porcelain", "--untracked-files=all", "--ignored=matching"):
                _fail("Cached dependency context is dirty")
            branch_tip = _git(path, "rev-parse", "--verify", f"refs/remotes/origin/{dep['branch']}^{{commit}}")
            _git(path, "merge-base", "--is-ancestor", commit, branch_tip)
            version_tag, version = _version(path, dep)
        if any(line.startswith("160000 ") for line in _git(path, "ls-files", "--stage").splitlines()):
            _fail("Submodule gitlinks are unsupported")
        _material_hash(path)  # Unsupported materials outside context also fail.
        context = _safe_path(path / dep["subdirectory"])
        if not context.is_dir():
            _fail("Dependency subdirectory does not exist")
        records.append({"name": name, "repository": repository, "commit": commit,
                        "version_tag": version_tag, "version": version,
                        "subdirectory": dep["subdirectory"],
                        "material_hash": _material_hash(context), "publishable": not local,
                        "context_path": str(context),
                        "audit": {"branch": dep["branch"], "initial_branch_tip": branch_tip,
                                  "local_path": str(path) if local else None}})
    return records


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def material_lock(source_sha, config, dependency_records, base_digest, builder_sha) -> dict:
    """Build identity. Caller restricts null base_digest to root FROM scratch."""
    config = validate_config(config, ci=False)
    _sha(source_sha, "source_sha")
    _sha(builder_sha, "builder_sha")
    if base_digest is not None and (not isinstance(base_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", base_digest)):
        _fail("base_digest must be an OCI sha256 digest")
    by_name = {r["name"]: r for r in dependency_records}
    if len(by_name) != len(dependency_records) or set(by_name) != set(config["dependencies"]):
        _fail("Dependency records must match config exactly")
    materials = []
    for name, dep in sorted(config["dependencies"].items()):
        record = by_name[name]
        for field in ("repository", "commit", "subdirectory"):
            if record.get(field) != dep[field]:
                _fail("Dependency record does not match config pin")
        if dep.get("version_tag", record.get("version_tag")) != record.get("version_tag"):
            _fail("Dependency record version_tag mismatch")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", record.get("material_hash", "")):
            _fail("Dependency material hash is missing or invalid")
        if type(record.get("publishable")) is not bool or record["publishable"] == ("local-path" in dep):
            _fail("Dependency publishability mismatch")
        materials.append({k: record[k] for k in ("name", "repository", "commit", "version_tag",
                                                "version", "subdirectory", "material_hash", "publishable")})
    return {"schemaVersion": 1, "source_sha": source_sha, "builder_sha": builder_sha,
            "base_digest": base_digest, "build": config["build"], "dependencies": materials,
            "publishable": all(r["publishable"] for r in materials)}


def lock_hash(lock) -> str:
    return "sha256:" + hashlib.sha256(_canonical(lock)).hexdigest()
