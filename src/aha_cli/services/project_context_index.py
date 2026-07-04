from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable
import uuid

from aha_cli.domain.models import default_project_context_index_config, utc_now
from aha_cli.store.io import iter_jsonl_from, read_json, write_json
from aha_cli.store.knowledge import project_key as derive_project_key
from aha_cli.store.paths import aha_home_path


INDEX_SCHEMA_VERSION = 1
INDEX_DIR = "project_context"
INDEX_FILE = "index.json"
SUMMARY_FILE = "summary.md"
INDEX_SECTIONS = ("packages", "symbols", "build", "configs", "device_tree", "tests", "entry_points")
RECORD_SECTIONS = ("files", *INDEX_SECTIONS)
SHARD_STORAGE_FORMAT = "sharded-jsonl"
SHARD_STORAGE_VERSION = 1
ROOT_REPO_ID = "root"
REPOS_DIR = "repos"

ExtractorRun = Callable[[Path, list[dict], list[str], dict], dict[str, list[dict]]]


@dataclass(frozen=True)
class ProjectContextExtractor:
    name: str
    flavors: tuple[str, ...]
    sections: tuple[str, ...]
    run: ExtractorRun

    def matches(self, flavors: list[str]) -> bool:
        if "*" in self.flavors:
            return True
        active = set(flavors)
        return any(flavor in active for flavor in self.flavors)


_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".aha",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "output",
    "out",
    ".eggs",
    "htmlcov",
    ".tox",
    "site-packages",
}
_C_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".s", ".S"}
_DTS_EXTENSIONS = {".dts", ".dtsi"}
_CLOUD_NAMES = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".gitlab-ci.yml",
    ".github/workflows",
}

_GENERIC_TOPDIR_PRIORITY = {
    "src": 2,
    "include": 3,
    "tests": 4,
    "test": 4,
    "docs": 6,
    "third_party": 9,
    "vendor": 9,
}
_PROFILE_TOPDIR_PRIORITY = {
    "buildroot-external": {
        "platform": 0,
        "app_source": 1,
        "fw_bsp": 2,
        "docs": 4,
        "prebuild": 6,
        "buildroot-dist": 8,
    },
    "linux-kernel": {
        "drivers": 0,
        "arch": 1,
        "include": 2,
        "kernel": 3,
        "net": 3,
        "fs": 3,
        "Documentation": 6,
        "tools": 7,
    },
    "uboot": {
        "board": 0,
        "configs": 1,
        "include": 2,
        "arch": 3,
        "drivers": 4,
        "cmd": 5,
        "common": 5,
    },
    "embedded-c-app": {
        "app_source": 0,
        "src": 1,
        "include": 2,
        "platform": 3,
        "fw_bsp": 4,
        "docs": 6,
    },
    "cloud": {
        ".github": 0,
        "deploy": 1,
        "k8s": 1,
        "helm": 1,
        "terraform": 1,
        "src": 3,
        "app": 3,
    },
}
_LOW_PRIORITY_PATH_MARKERS = (
    "/third-party/",
    "/ThirdLibrary/",
    "/opensource/",
    "/test/unit-test/",
    "/test/cbmc/",
)


def project_context_index_config(config: dict | None) -> dict:
    raw_knowledge = (config or {}).get("knowledge") if isinstance(config, dict) else {}
    raw = raw_knowledge.get("project_context_index") if isinstance(raw_knowledge, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    cfg = default_project_context_index_config() | raw
    for key, default in (
        ("max_files", 20000),
        ("max_file_bytes", 2 * 1024 * 1024),
        ("max_records_per_extractor", 20000),
    ):
        try:
            cfg[key] = max(1, int(cfg.get(key) or default))
        except (TypeError, ValueError):
            cfg[key] = default
    cfg["include_untracked"] = bool(cfg.get("include_untracked", False))
    cfg["prompt_injection_enabled"] = bool(cfg.get("prompt_injection_enabled", False))
    return cfg


def workspace_id_for(workspace: Path) -> str:
    resolved = Path(workspace).expanduser().resolve()
    return hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:12]


def project_context_index_dir(root: Path, project_key_value: str, workspace_id: str) -> Path:
    return aha_home_path(root) / "runtime" / INDEX_DIR / project_key_value / workspace_id


def project_context_index_paths(root: Path, project_key_value: str, workspace_id: str) -> dict[str, Path]:
    base = project_context_index_dir(root, project_key_value, workspace_id)
    return {
        "dir": base,
        "index": base / INDEX_FILE,
        "summary": base / SUMMARY_FILE,
    }


def _run_git_raw(workspace: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _run_git(workspace: Path, *args: str) -> str:
    return _run_git_raw(workspace, *args).strip()


def _git_toplevel(workspace: Path) -> str:
    return _run_git(workspace, "rev-parse", "--show-toplevel")


def _git_short_status(workspace: Path) -> str:
    return _run_git(workspace, "status", "--porcelain=v1")


def _safe_repo_id(relpath: str) -> str:
    if not relpath:
        return ROOT_REPO_ID
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", relpath.strip("/")).strip("_")
    if not slug:
        slug = "repo"
    digest = hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:80]}_{digest}"


def _detect_repository_profiles(workspace: Path, paths: list[str], kinds: set[str] | None = None) -> list[str]:
    path_set = set(paths)
    kind_set = kinds or set()
    profiles: list[str] = []

    def add(profile: str) -> None:
        if profile not in profiles:
            profiles.append(profile)

    if any(path.startswith("platform/") for path in path_set) and any(
        path.startswith(("buildroot-dist/", "platform/")) and (path.endswith(".mk") or Path(path).name == "Config.in")
        for path in path_set
    ):
        add("buildroot-external")
    if (
        (workspace / "MAINTAINERS").exists()
        and any(path.startswith("drivers/") for path in path_set)
        and any(path.startswith("arch/") for path in path_set)
    ):
        add("linux-kernel")
    if any(path.startswith("configs/") and path.endswith("_defconfig") for path in path_set) and (
        any(path.startswith("board/") for path in path_set) or (workspace / "include" / "configs").is_dir()
    ):
        add("uboot")
    if "c" in kind_set or any(path.startswith("app_source/") and Path(path).suffix in _C_EXTENSIONS for path in path_set):
        add("embedded-c-app")
    if "python" in kind_set or (workspace / "pyproject.toml").exists():
        add("python-service")
    if "cloud" in kind_set or any(path.startswith(".github/workflows/") for path in path_set):
        add("cloud")
    return profiles or ["generic"]


def _path_priority(relpath: str, profiles: list[str] | tuple[str, ...] | None = None) -> int:
    normalized = relpath.replace("\\", "/")
    top = normalized.split("/", 1)[0] if normalized else ""
    profile_priorities = []
    for profile in profiles or ():
        priorities = _PROFILE_TOPDIR_PRIORITY.get(profile) or {}
        if top in priorities:
            profile_priorities.append(priorities[top])
    priority = min(profile_priorities) if profile_priorities else _GENERIC_TOPDIR_PRIORITY.get(top, 5)
    if any(marker in f"/{normalized}/" for marker in _LOW_PRIORITY_PATH_MARKERS):
        priority += 4
    return priority


def _path_sort_key(relpath: str, profiles: list[str] | tuple[str, ...] | None = None) -> tuple[int, str]:
    return (_path_priority(relpath, profiles), relpath)


def _path_bucket(relpath: str) -> str:
    normalized = relpath.replace("\\", "/")
    return normalized.split("/", 1)[0] if "/" in normalized else "."


def _budget_candidates(
    candidates: list[tuple[str, str, str]],
    max_files: int,
    *,
    profiles: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, str, str]]:
    if len(candidates) <= max_files:
        return candidates
    per_bucket_cap = max(500, max_files // 4)
    selected: list[tuple[str, str, str]] = []
    deferred: list[tuple[str, str, str]] = []
    bucket_counts: dict[str, int] = {}
    for candidate in candidates:
        bucket = _path_bucket(candidate[0])
        count = bucket_counts.get(bucket, 0)
        if count < per_bucket_cap and len(selected) < max_files:
            selected.append(candidate)
            bucket_counts[bucket] = count + 1
        else:
            deferred.append(candidate)
    seen = {candidate[0] for candidate in selected}
    for candidate in deferred:
        if len(selected) >= max_files:
            break
        if candidate[0] in seen:
            continue
        selected.append(candidate)
        seen.add(candidate[0])
    selected.sort(key=lambda item: _path_sort_key(item[0], profiles))
    return selected


def _bucket_counts(paths: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        bucket = _path_bucket(path)
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def discover_project_repos(workspace: Path) -> list[dict]:
    workspace = Path(workspace).expanduser().resolve()
    toplevel = _git_toplevel(workspace)
    if not toplevel:
        return [
            {
                "id": ROOT_REPO_ID,
                "role": "filesystem",
                "path": "",
                "abs_path": str(workspace),
                "initialized": True,
                "git_head": "",
                "git_status_fingerprint": "",
            }
        ]
    root = Path(toplevel).resolve()
    repos: list[dict] = [
        {
            "id": ROOT_REPO_ID,
            "role": "root",
            "path": "",
            "abs_path": str(root),
            "initialized": True,
            "git_head": _git_head(root),
            "git_status_fingerprint": _git_status_fingerprint(root),
        }
    ]
    seen = {""}
    out = _run_git_raw(root, "submodule", "status", "--recursive")
    for line in out.splitlines():
        if not line.strip():
            continue
        state = line[0] if line[0] in {" ", "-", "+", "U"} else " "
        rest = line[1:].strip() if state in {" ", "-", "+", "U"} else line.strip()
        parts = rest.split()
        if len(parts) < 2:
            continue
        relpath = parts[1].strip()
        if not relpath or relpath in seen:
            continue
        seen.add(relpath)
        repo_path = root / relpath
        initialized = state != "-" and repo_path.exists() and bool(_git_toplevel(repo_path))
        repos.append(
            {
                "id": _safe_repo_id(relpath),
                "role": "submodule",
                "path": relpath,
                "abs_path": str(repo_path),
                "initialized": initialized,
                "submodule_state": state,
                "git_head": _git_head(repo_path) if initialized else parts[0],
                "git_status_fingerprint": _git_status_fingerprint(repo_path) if initialized else "",
            }
        )
    return repos


def _public_repos(repos: list[dict]) -> list[dict]:
    public: list[dict] = []
    for repo in repos:
        item = {key: value for key, value in repo.items() if key != "abs_path"}
        public.append(item)
    return public


def _repo_fingerprint(repos: list[dict], *, include_status: bool = True) -> str:
    digest = hashlib.sha1()
    for repo in sorted(repos, key=lambda item: str(item.get("path") or "")):
        for key in ("id", "role", "path", "initialized", "git_head"):
            digest.update(str(repo.get(key) or "").encode("utf-8"))
            digest.update(b"\0")
        if include_status:
            digest.update(str(repo.get("git_status_fingerprint") or "").encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest() if repos else ""


def _git_head(workspace: Path) -> str:
    return _run_git(workspace, "rev-parse", "HEAD")


def _git_status_fingerprint(workspace: Path) -> str:
    status = _git_short_status(workspace)
    if not status:
        return ""
    return hashlib.sha1(status.encode("utf-8")).hexdigest()


def _git_files(workspace: Path, *, include_untracked: bool) -> list[str]:
    args = ["ls-files", "-z", "--cached"]
    if include_untracked:
        args.extend(["--others", "--exclude-standard"])
    out = _run_git(workspace, *args)
    if "\0" in out:
        return [part.strip() for part in out.split("\0") if part.strip()]
    return [line.strip() for line in out.splitlines() if line.strip()]


def _skip_path(path: Path, workspace: Path, max_file_bytes: int) -> bool:
    try:
        rel = path.relative_to(workspace)
    except ValueError:
        return True
    if any(part in _IGNORE_DIRS for part in rel.parts):
        return True
    try:
        return not path.is_file() or path.stat().st_size > max_file_bytes
    except OSError:
        return True


def _filesystem_files(workspace: Path, *, max_files: int, max_file_bytes: int) -> list[str]:
    files: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if len(files) >= max_files:
            break
        if _skip_path(path, workspace, max_file_bytes):
            continue
        files.append(path.relative_to(workspace).as_posix())
    return files


def _file_kind(relpath: str) -> str:
    path = Path(relpath)
    name = path.name
    suffix = path.suffix
    if name in {"Kconfig", "Config.in"}:
        return "config"
    if name == ".config" or name.endswith("_defconfig") or suffix in {".config", ".fragment"}:
        return "config"
    if name in {"Makefile", "Kbuild"} or suffix == ".mk":
        return "build"
    if suffix in _DTS_EXTENSIONS:
        return "device_tree"
    if suffix in _C_EXTENSIONS:
        return "c"
    if suffix == ".py":
        return "python"
    if suffix in {".go"}:
        return "go"
    if suffix in {".rs"}:
        return "rust"
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return "typescript"
    if suffix in {".yaml", ".yml", ".tf"} or name in _CLOUD_NAMES:
        return "cloud"
    if "test" in name.lower() or relpath.startswith(("tests/", "test/")):
        return "test"
    return "file"


def _file_record(workspace: Path, relpath: str, *, repo_id: str = ROOT_REPO_ID, repo_path: str = "") -> dict | None:
    path = workspace / relpath
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": relpath,
        "kind": _file_kind(relpath),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "ext": path.suffix,
        "repo": repo_id,
        "repo_path": repo_path,
    }


def collect_project_files(workspace: Path, config: dict | None = None) -> tuple[list[dict], dict]:
    cfg = project_context_index_config(config)
    workspace = Path(workspace).expanduser().resolve()
    max_files = int(cfg["max_files"])
    max_file_bytes = int(cfg["max_file_bytes"])
    repos = discover_project_repos(workspace)
    git_repos = [repo for repo in repos if repo.get("role") in {"root", "submodule"}]
    source = "git-tracked" if git_repos else "filesystem"
    candidates: list[tuple[str, str, str]] = []
    if git_repos:
        for repo in git_repos:
            if not repo.get("initialized"):
                continue
            repo_path = Path(str(repo.get("abs_path") or ""))
            repo_rel = str(repo.get("path") or "")
            for relpath in _git_files(repo_path, include_untracked=bool(cfg["include_untracked"])):
                full_rel = f"{repo_rel}/{relpath}" if repo_rel else relpath
                candidates.append((full_rel, str(repo.get("id") or ROOT_REPO_ID), repo_rel))
    else:
        for relpath in _filesystem_files(workspace, max_files=max_files, max_file_bytes=max_file_bytes):
            candidates.append((relpath, ROOT_REPO_ID, ""))
    candidate_paths = [item[0] for item in candidates]
    candidate_kinds = {_file_kind(path) for path in candidate_paths}
    profiles = _detect_repository_profiles(workspace, candidate_paths, candidate_kinds)
    candidates.sort(key=lambda item: _path_sort_key(item[0], profiles))
    selected_candidates = _budget_candidates(candidates, max_files, profiles=profiles)
    selected_paths = {candidate[0] for candidate in selected_candidates}
    records: list[dict] = []
    skipped = max(0, len(candidates) - len(selected_candidates))
    for relpath, repo_id, repo_path in selected_candidates:
        path = workspace / relpath
        if _skip_path(path, workspace, max_file_bytes):
            skipped += 1
            continue
        record = _file_record(workspace, relpath, repo_id=repo_id, repo_path=repo_path)
        if record is None:
            skipped += 1
            continue
        records.append(record)
    records.sort(key=lambda item: _path_sort_key(str(item.get("path") or ""), profiles))
    return records, {
        "source": source,
        "skipped": skipped,
        "candidate_files": len(candidates),
        "max_files": max_files,
        "max_file_bytes": max_file_bytes,
        "include_untracked": bool(cfg["include_untracked"]),
        "repos": _public_repos(repos),
        "profiles": profiles,
        "repo_fingerprint": _repo_fingerprint(repos),
        "bucket_counts": _bucket_counts(selected_paths),
    }


def detect_project_flavors(workspace: Path, files: list[dict] | None = None) -> list[str]:
    workspace = Path(workspace).expanduser().resolve()
    paths = {str(item.get("path") or "") for item in (files or [])}
    kinds = {str(item.get("kind") or "") for item in (files or [])}
    flavors: list[str] = []

    def add(name: str) -> None:
        if name not in flavors:
            flavors.append(name)

    if any(path.endswith((".c", ".h", ".cc", ".cpp", ".hpp")) for path in paths) or "c" in kinds:
        add("c")
    if "config" in kinds or any(Path(path).name in {"Kconfig", "Config.in"} for path in paths):
        add("kconfig")
    if any(Path(path).suffix in _DTS_EXTENSIONS for path in paths):
        add("dts")
    if (workspace / "MAINTAINERS").exists() and (workspace / "drivers").is_dir() and (workspace / "arch").is_dir():
        add("linux-kernel")
    if (workspace / "include" / "configs").is_dir() and ((workspace / "cmd").is_dir() or (workspace / "common").is_dir()):
        add("uboot")
    if (workspace / "package").is_dir() and (workspace / "Config.in").exists() and any(path.startswith("package/") for path in paths):
        add("buildroot")
    if "python" in kinds or (workspace / "pyproject.toml").exists():
        add("python")
    if (workspace / "go.mod").exists() or "go" in kinds:
        add("go")
    if (workspace / "Cargo.toml").exists() or "rust" in kinds:
        add("rust")
    if (workspace / "package.json").exists() or "typescript" in kinds:
        add("node")
    if "cloud" in kinds or any(path.startswith(".github/workflows/") for path in paths):
        add("cloud")
    return flavors or ["generic"]


def _empty_extractor_result(
    workspace: Path,
    files: list[dict],
    flavors: list[str],
    config: dict,
) -> dict[str, list[dict]]:
    del workspace, files, flavors, config
    return {}


_C_SKIP_CALL_NAMES = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "case",
}
_C_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_C_FUNCTION_RE = re.compile(
    rf"^\s*(?:static\s+|inline\s+|extern\s+|const\s+|volatile\s+|unsigned\s+|signed\s+|struct\s+|enum\s+|union\s+|long\s+|short\s+|void\s+|int\s+|char\s+|bool\s+|u\d+\s+|s\d+\s+|[A-Za-z_][A-Za-z0-9_\*\s]+\s+)+(?P<name>{_C_IDENTIFIER})\s*\([^;]*\)\s*(?:\{{|$)"
)
_C_MACRO_RE = re.compile(rf"^\s*#\s*define\s+(?P<name>{_C_IDENTIFIER})(?P<args>\([^)]*\))?")
_C_TYPE_RE = re.compile(rf"^\s*(?:typedef\s+)?(?P<kind>struct|enum|union)\s+(?P<name>{_C_IDENTIFIER})\b")
_KCONFIG_START_RE = re.compile(r"^\s*(?P<kind>config|menuconfig)\s+(?P<name>[A-Za-z0-9_]+)\b")
_KCONFIG_PROMPT_RE = re.compile(r'^\s*(?:bool|tristate|string|int|hex)\s+"(?P<prompt>[^"]+)"')
_KCONFIG_DEPENDS_RE = re.compile(r"^\s*depends\s+on\s+(?P<expr>.+)")
_KCONFIG_SELECT_RE = re.compile(r"^\s*select\s+(?P<name>[A-Za-z0-9_]+)(?:\s+if\s+(?P<expr>.+))?")
_CONFIG_ASSIGN_RE = re.compile(r"^\s*(?P<name>(?:BR2|CONFIG)_[A-Za-z0-9_]+)=(?P<value>.*)")
_CONFIG_NOT_SET_RE = re.compile(r"^\s*#\s*(?P<name>(?:BR2|CONFIG)_[A-Za-z0-9_]+)\s+is\s+not\s+set")
_MAKE_ASSIGN_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9_.$(){}+-]+)\s*(?P<op>[:+?]?=|\+=)\s*(?P<value>.*)")
_MAKE_INCLUDE_RE = re.compile(r"^\s*-?include\s+(?P<value>.+)")
_DTS_LABEL_RE = re.compile(rf"^\s*(?P<label>{_C_IDENTIFIER})\s*:\s*(?P<node>[A-Za-z0-9,._+-]+(?:@[A-Fa-f0-9x]+)?)\s*\{{")
_DTS_NODE_RE = re.compile(r"^\s*(?P<node>[A-Za-z0-9,._+-]+(?:@[A-Fa-f0-9x]+)?)\s*\{")
_DTS_COMPAT_RE = re.compile(r"compatible\s*=\s*(?P<value>[^;]+);")
_DTS_INCLUDE_RE = re.compile(r'^\s*(?:#include\s+[<"](?P<cpp>[^>"]+)[>"]|/include/\s+"(?P<dts>[^"]+)")')
_DTS_STRING_RE = re.compile(r'"([^"]+)"')


def _record_limit(config: dict) -> int:
    try:
        return max(1, int(config.get("max_records_per_extractor") or 20000)) * 4
    except (TypeError, ValueError):
        return 80000


def _final_record_limit(config: dict) -> int:
    try:
        return max(1, int(config.get("max_records_per_extractor") or 20000))
    except (TypeError, ValueError):
        return 20000


def _budget_section_records(
    records: list[dict],
    limit: int,
    *,
    profiles: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    if len(records) <= limit:
        return sorted(records, key=lambda item: _path_sort_key(str(item.get("path") or ""), profiles))
    sorted_records = sorted(records, key=lambda item: _path_sort_key(str(item.get("path") or ""), profiles))
    per_bucket_cap = max(1, limit // 4)
    selected: list[dict] = []
    deferred: list[dict] = []
    bucket_counts: dict[str, int] = {}
    for record in sorted_records:
        bucket = _path_bucket(str(record.get("path") or ""))
        count = bucket_counts.get(bucket, 0)
        if count < per_bucket_cap and len(selected) < limit:
            selected.append(record)
            bucket_counts[bucket] = count + 1
        else:
            deferred.append(record)
    seen = {id(record) for record in selected}
    for record in deferred:
        if len(selected) >= limit:
            break
        if id(record) in seen:
            continue
        selected.append(record)
        seen.add(id(record))
    selected.sort(key=lambda item: _path_sort_key(str(item.get("path") or ""), profiles))
    return selected


def _read_project_text(workspace: Path, relpath: str, config: dict) -> str:
    path = workspace / relpath
    max_bytes = int(config.get("max_file_bytes") or 2 * 1024 * 1024)
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _files_of_kind(files: list[dict], *kinds: str, profiles: list[str] | tuple[str, ...] | None = None) -> list[str]:
    wanted = set(kinds)
    return sorted(
        [
            str(item.get("path") or "")
            for item in files
            if str(item.get("kind") or "") in wanted and str(item.get("path") or "")
        ],
        key=lambda path: _path_sort_key(path, profiles),
    )


def _append_limited(records: list[dict], record: dict, limit: int) -> bool:
    if len(records) >= limit:
        return False
    records.append(record)
    return True


def _c_symbol_extractor(workspace: Path, files: list[dict], flavors: list[str], config: dict) -> dict[str, list[dict]]:
    del flavors
    records: list[dict] = []
    limit = _record_limit(config)
    profiles = config.get("profiles") if isinstance(config.get("profiles"), list) else None
    for relpath in _files_of_kind(files, "c", profiles=profiles):
        text = _read_project_text(workspace, relpath, config)
        if not text:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            macro = _C_MACRO_RE.match(line)
            if macro:
                if not _append_limited(
                    records,
                    {
                        "kind": "macro",
                        "language": "c",
                        "name": macro.group("name"),
                        "path": relpath,
                        "line": index + 1,
                        "signature": stripped[:240],
                    },
                    limit,
                ):
                    return {"symbols": records}
                continue
            c_type = _C_TYPE_RE.match(line)
            if c_type:
                if not _append_limited(
                    records,
                    {
                        "kind": c_type.group("kind"),
                        "language": "c",
                        "name": c_type.group("name"),
                        "path": relpath,
                        "line": index + 1,
                        "signature": stripped[:240],
                    },
                    limit,
                ):
                    return {"symbols": records}
                continue
            if stripped.endswith(";") or stripped.startswith("#"):
                continue
            function_line = line
            if stripped.endswith(")") and index + 1 < len(lines) and lines[index + 1].strip().startswith("{"):
                function_line = f"{line} {{"
            match = _C_FUNCTION_RE.match(function_line)
            if not match:
                continue
            name = match.group("name")
            if name in _C_SKIP_CALL_NAMES:
                continue
            if not _append_limited(
                records,
                {
                    "kind": "function",
                    "language": "c",
                    "name": name,
                    "path": relpath,
                    "line": index + 1,
                    "signature": stripped[:240],
                },
                limit,
            ):
                return {"symbols": records}
    return {"symbols": records}


def _kconfig_extractor(workspace: Path, files: list[dict], flavors: list[str], config: dict) -> dict[str, list[dict]]:
    del flavors
    records: list[dict] = []
    limit = _record_limit(config)
    profiles = config.get("profiles") if isinstance(config.get("profiles"), list) else None
    for relpath in _files_of_kind(files, "config", profiles=profiles):
        text = _read_project_text(workspace, relpath, config)
        if not text:
            continue
        current: dict | None = None

        def flush() -> bool:
            nonlocal current
            if current is None:
                return True
            ok = _append_limited(records, current, limit)
            current = None
            return ok

        for index, line in enumerate(text.splitlines()):
            not_set = _CONFIG_NOT_SET_RE.match(line)
            if not_set:
                if not _append_limited(
                    records,
                    {
                        "kind": "config-value",
                        "name": not_set.group("name"),
                        "path": relpath,
                        "line": index + 1,
                        "value": "not set",
                    },
                    limit,
                ):
                    return {"configs": records}
                continue
            assignment = _CONFIG_ASSIGN_RE.match(line)
            if assignment:
                if not _append_limited(
                    records,
                    {
                        "kind": "config-value",
                        "name": assignment.group("name"),
                        "path": relpath,
                        "line": index + 1,
                        "value": assignment.group("value").strip()[:300],
                    },
                    limit,
                ):
                    return {"configs": records}
                continue
            start = _KCONFIG_START_RE.match(line)
            if start:
                if not flush():
                    return {"configs": records}
                current = {
                    "kind": start.group("kind"),
                    "name": start.group("name"),
                    "path": relpath,
                    "line": index + 1,
                    "prompt": "",
                    "depends": [],
                    "selects": [],
                }
                continue
            if current is None:
                continue
            prompt = _KCONFIG_PROMPT_RE.match(line)
            if prompt and not current.get("prompt"):
                current["prompt"] = prompt.group("prompt")
                continue
            depends = _KCONFIG_DEPENDS_RE.match(line)
            if depends:
                current.setdefault("depends", []).append(depends.group("expr").strip())
                continue
            select = _KCONFIG_SELECT_RE.match(line)
            if select:
                item = {"name": select.group("name")}
                if select.group("expr"):
                    item["if"] = select.group("expr").strip()
                current.setdefault("selects", []).append(item)
        if not flush():
            return {"configs": records}
    return {"configs": records}


def _build_file_extractor(workspace: Path, files: list[dict], flavors: list[str], config: dict) -> dict[str, list[dict]]:
    del flavors
    records: list[dict] = []
    limit = _record_limit(config)
    buildroot_suffixes = (
        "_DEPENDENCIES",
        "_SELECTS",
        "_CONF_OPTS",
        "_VERSION",
        "_SITE",
        "_LICENSE",
        "_INSTALL_TARGET",
    )
    profiles = config.get("profiles") if isinstance(config.get("profiles"), list) else None
    for relpath in _files_of_kind(files, "build", profiles=profiles):
        text = _read_project_text(workspace, relpath, config)
        if not text:
            continue
        for index, line in enumerate(text.splitlines()):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            include = _MAKE_INCLUDE_RE.match(line)
            if include:
                if not _append_limited(
                    records,
                    {
                        "kind": "include",
                        "name": "include",
                        "path": relpath,
                        "line": index + 1,
                        "value": include.group("value").strip(),
                    },
                    limit,
                ):
                    return {"build": records}
                continue
            assign = _MAKE_ASSIGN_RE.match(line)
            if not assign:
                continue
            name = assign.group("name").strip()
            value = assign.group("value").strip()
            record_kind = ""
            if name.startswith(("obj-", "lib-", "subdir-")):
                record_kind = "kbuild-object"
            elif name.endswith(buildroot_suffixes) or "BUILDROOT" in name:
                record_kind = "buildroot-var"
            elif relpath.endswith(".mk") and "_" in name:
                record_kind = "make-var"
            if not record_kind:
                continue
            if not _append_limited(
                records,
                {
                    "kind": record_kind,
                    "name": name,
                    "path": relpath,
                    "line": index + 1,
                    "op": assign.group("op"),
                    "value": value[:300],
                },
                limit,
            ):
                return {"build": records}
    return {"build": records}


def _device_tree_extractor(workspace: Path, files: list[dict], flavors: list[str], config: dict) -> dict[str, list[dict]]:
    del flavors
    records: list[dict] = []
    limit = _record_limit(config)
    profiles = config.get("profiles") if isinstance(config.get("profiles"), list) else None
    for relpath in _files_of_kind(files, "device_tree", profiles=profiles):
        text = _read_project_text(workspace, relpath, config)
        if not text:
            continue
        for index, line in enumerate(text.splitlines()):
            include = _DTS_INCLUDE_RE.match(line)
            if include:
                target = (include.group("cpp") or include.group("dts") or "").strip()
                if target and not _append_limited(
                    records,
                    {"kind": "include", "name": target, "path": relpath, "line": index + 1},
                    limit,
                ):
                    return {"device_tree": records}
            compat = _DTS_COMPAT_RE.search(line)
            if compat:
                for value in _DTS_STRING_RE.findall(compat.group("value")):
                    if not _append_limited(
                        records,
                        {"kind": "compatible", "name": value, "path": relpath, "line": index + 1},
                        limit,
                    ):
                        return {"device_tree": records}
                continue
            label = _DTS_LABEL_RE.match(line)
            if label:
                if not _append_limited(
                    records,
                    {
                        "kind": "label",
                        "name": label.group("label"),
                        "node": label.group("node"),
                        "path": relpath,
                        "line": index + 1,
                    },
                    limit,
                ):
                    return {"device_tree": records}
                continue
            node = _DTS_NODE_RE.match(line)
            if node and not line.strip().startswith(("/", "&")):
                node_name = node.group("node")
                if node_name not in {"if", "for"} and not _append_limited(
                    records,
                    {"kind": "node", "name": node_name, "path": relpath, "line": index + 1},
                    limit,
                ):
                    return {"device_tree": records}
    return {"device_tree": records}


def default_project_context_extractors() -> list[ProjectContextExtractor]:
    return [
        ProjectContextExtractor("c-symbols", ("c",), ("symbols",), _c_symbol_extractor),
        ProjectContextExtractor("kconfig", ("kconfig", "buildroot"), ("configs",), _kconfig_extractor),
        ProjectContextExtractor(
            "build-files",
            ("c", "kconfig", "linux-kernel", "uboot", "buildroot"),
            ("build",),
            _build_file_extractor,
        ),
        ProjectContextExtractor("device-tree", ("dts",), ("device_tree",), _device_tree_extractor),
        ProjectContextExtractor("python", ("python",), ("symbols", "entry_points"), _empty_extractor_result),
        ProjectContextExtractor("cloud", ("cloud",), ("entry_points",), _empty_extractor_result),
    ]


def _empty_index_sections() -> dict[str, list[dict]]:
    return {section: [] for section in INDEX_SECTIONS}


def run_project_context_extractors(
    workspace: Path,
    files: list[dict],
    flavors: list[str],
    *,
    profiles: list[str] | None = None,
    config: dict | None = None,
    extractors: list[ProjectContextExtractor] | None = None,
) -> dict:
    cfg = project_context_index_config(config)
    cfg["profiles"] = list(profiles or [])
    final_limit = _final_record_limit(cfg)
    sections = _empty_index_sections()
    statuses: list[dict] = []
    errors: list[dict] = []
    active_extractors = extractors if extractors is not None else default_project_context_extractors()
    for extractor in active_extractors:
        if not extractor.matches(flavors):
            statuses.append(
                {
                    "name": extractor.name,
                    "status": "skipped",
                    "flavors": list(extractor.flavors),
                    "sections": list(extractor.sections),
                    "records": 0,
                }
            )
            continue
        try:
            result = extractor.run(workspace, files, flavors, cfg)
        except Exception as exc:  # noqa: BLE001 - one extractor must not break the map.
            error = {"extractor": extractor.name, "error": f"{type(exc).__name__}: {exc}"}
            errors.append(error)
            statuses.append(
                {
                    "name": extractor.name,
                    "status": "failed",
                    "flavors": list(extractor.flavors),
                    "sections": list(extractor.sections),
                    "records": 0,
                    "error": error["error"],
                }
            )
            continue
        record_count = 0
        if isinstance(result, dict):
            for section in extractor.sections:
                records = result.get(section, [])
                if not isinstance(records, list):
                    continue
                section_records = _budget_section_records(
                    [record for record in records if isinstance(record, dict)],
                    final_limit,
                    profiles=profiles,
                )
                sections[section].extend(section_records)
                record_count += len(section_records)
        statuses.append(
            {
                "name": extractor.name,
                "status": "ok",
                "flavors": list(extractor.flavors),
                "sections": list(extractor.sections),
                "records": record_count,
            }
        )
    return {"sections": sections, "extractors": statuses, "errors": errors}


def _tool_status(command: str) -> dict:
    path = shutil.which(command)
    return {"available": bool(path), "path": path or ""}


def _counts(index: dict) -> dict:
    manifest_counts = index.get("counts")
    if isinstance(manifest_counts, dict) and not isinstance(index.get("files"), list):
        by_kind = manifest_counts.get("by_kind") if isinstance(manifest_counts.get("by_kind"), dict) else {}
        return {
            "files": int(manifest_counts.get("files") or 0),
            "packages": int(manifest_counts.get("packages") or 0),
            "symbols": int(manifest_counts.get("symbols") or 0),
            "build": int(manifest_counts.get("build") or 0),
            "configs": int(manifest_counts.get("configs") or 0),
            "device_tree": int(manifest_counts.get("device_tree") or 0),
            "tests": int(manifest_counts.get("tests") or 0),
            "entry_points": int(manifest_counts.get("entry_points") or 0),
            "extractor_errors": int(manifest_counts.get("extractor_errors") or 0),
            "by_kind": {str(kind): int(count) for kind, count in by_kind.items()},
        }
    files = index.get("files") if isinstance(index.get("files"), list) else []
    by_kind: dict[str, int] = {}
    for item in files:
        kind = str(item.get("kind") or "file")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "files": len(files),
        "packages": len(index.get("packages") or []),
        "symbols": len(index.get("symbols") or []),
        "build": len(index.get("build") or []),
        "configs": len(index.get("configs") or []),
        "device_tree": len(index.get("device_tree") or []),
        "tests": len(index.get("tests") or []),
        "entry_points": len(index.get("entry_points") or []),
        "extractor_errors": len(index.get("errors") or []),
        "by_kind": by_kind,
    }


def _workspace_fingerprint(files: list[dict]) -> str:
    digest = hashlib.sha1()
    for item in sorted(files, key=lambda record: str(record.get("path") or "")):
        digest.update(str(item.get("path") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.get("size") or 0).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.get("mtime") or 0).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest() if files else ""


def _buildroot_package_dir(path: str) -> str:
    parts = Path(path).parts
    if "package" not in parts:
        return ""
    index = parts.index("package")
    if len(parts) <= index + 1:
        return ""
    if Path(path).name == "Config.in" or Path(path).suffix == ".mk":
        return "/".join(parts[:-1])
    return ""


def _buildroot_package_name(path: str) -> str:
    suffix = Path(path).suffix
    if suffix == ".mk":
        return Path(path).stem
    package_dir = _buildroot_package_dir(path)
    return Path(package_dir).name if package_dir else ""


def _symbol_to_package_name(symbol: str) -> str:
    for prefix in ("BR2_PACKAGE_", "CONFIG_PACKAGE_"):
        if symbol.startswith(prefix):
            return symbol.removeprefix(prefix).lower()
    return symbol.lower()


def _buildroot_dependency_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for token in re.split(r"\s+", value.strip()):
        token = token.strip()
        if not token or token == "\\" or "$" in token or token.startswith((",", ")")):
            continue
        token = token.strip("\\")
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _buildroot_package_records(files: list[dict], sections: dict[str, list[dict]]) -> list[dict]:
    packages: dict[str, dict] = {}

    def package_for_dir(package_dir: str, name: str = "") -> dict:
        key = package_dir or name
        if key not in packages:
            packages[key] = {
                "kind": "buildroot-package",
                "name": name or Path(package_dir).name,
                "path": package_dir,
                "package_dir": package_dir,
                "repo": ROOT_REPO_ID,
                "repo_path": "",
                "config_path": "",
                "mk_path": "",
                "config_symbols": [],
                "enabled_in": [],
                "dependencies": [],
                "variables": [],
            }
        elif name and not packages[key].get("name"):
            packages[key]["name"] = name
        return packages[key]

    file_repos = {
        str(item.get("path") or ""): (str(item.get("repo") or ROOT_REPO_ID), str(item.get("repo_path") or ""))
        for item in files
        if item.get("path")
    }
    symbol_to_packages: dict[str, set[str]] = {}

    for item in files:
        path = str(item.get("path") or "")
        package_dir = _buildroot_package_dir(path)
        if not package_dir:
            continue
        name = _buildroot_package_name(path)
        package = package_for_dir(package_dir, name)
        repo_id, repo_path = file_repos.get(path, (ROOT_REPO_ID, ""))
        package["repo"] = repo_id
        package["repo_path"] = repo_path
        if Path(path).name == "Config.in":
            package["config_path"] = path
        if Path(path).suffix == ".mk":
            package["mk_path"] = path
            package["path"] = path

    for record in sections.get("configs", []):
        path = str(record.get("path") or "")
        if record.get("kind") not in {"config", "menuconfig"}:
            continue
        package_dir = _buildroot_package_dir(path)
        if not package_dir:
            continue
        package = package_for_dir(package_dir, _buildroot_package_name(path))
        package["config_path"] = package.get("config_path") or path
        symbol = str(record.get("name") or "")
        if symbol and symbol not in package["config_symbols"]:
            package["config_symbols"].append(symbol)
            symbol_to_packages.setdefault(symbol, set()).add(package_dir)

    for record in sections.get("build", []):
        path = str(record.get("path") or "")
        package_dir = _buildroot_package_dir(path)
        if not package_dir:
            continue
        package = package_for_dir(package_dir, _buildroot_package_name(path))
        package["mk_path"] = package.get("mk_path") or path
        package["path"] = package.get("mk_path") or path
        name = str(record.get("name") or "")
        value = str(record.get("value") or "")
        if name.endswith("_DEPENDENCIES"):
            deps = _buildroot_dependency_tokens(value)
            for dep in deps:
                if dep not in package["dependencies"]:
                    package["dependencies"].append(dep)
        if name.endswith(("_VERSION", "_SITE", "_CONF_OPTS", "_INSTALL_TARGET")):
            variable = {"name": name, "value": value[:160], "line": record.get("line"), "path": path}
            package["variables"].append(variable)

    for record in sections.get("configs", []):
        if record.get("kind") != "config-value":
            continue
        symbol = str(record.get("name") or "")
        targets = set(symbol_to_packages.get(symbol, set()))
        guessed = _symbol_to_package_name(symbol)
        for package_dir, package in packages.items():
            if package.get("name") == guessed:
                targets.add(package_dir)
        for package_dir in targets:
            package = packages.get(package_dir)
            if not package:
                continue
            if len(package["enabled_in"]) >= 20:
                continue
            package["enabled_in"].append(
                {
                    "path": record.get("path"),
                    "line": record.get("line"),
                    "value": record.get("value"),
                }
            )

    records = [record for record in packages.values() if record.get("mk_path") or record.get("config_path")]
    for record in records:
        if not record.get("path"):
            record["path"] = record.get("mk_path") or record.get("config_path") or record.get("package_dir")
        record["config_symbols"] = sorted(record.get("config_symbols") or [])
        record["dependencies"] = sorted(record.get("dependencies") or [])
    records.sort(key=lambda item: _path_sort_key(str(item.get("path") or ""), ["buildroot-external"]))
    return records


def create_project_context_index_document(
    workspace: Path,
    *,
    project_key_value: str | None = None,
    config: dict | None = None,
    extractors: list[ProjectContextExtractor] | None = None,
) -> dict:
    workspace = Path(workspace).expanduser().resolve()
    files, collection = collect_project_files(workspace, config)
    flavors = detect_project_flavors(workspace, files)
    profiles = collection.get("profiles") if isinstance(collection.get("profiles"), list) else ["generic"]
    extractor_result = run_project_context_extractors(
        workspace,
        files,
        flavors,
        profiles=profiles,
        config=config,
        extractors=extractors,
    )
    sections = extractor_result["sections"]
    _annotate_section_repos(sections, files)
    sections["packages"] = _buildroot_package_records(files, sections)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_key": project_key_value or derive_project_key(workspace),
        "workspace": str(workspace),
        "workspace_id": workspace_id_for(workspace),
        "git_head": _git_head(workspace),
        "git_status_fingerprint": _git_status_fingerprint(workspace),
        "repo_fingerprint": collection.get("repo_fingerprint") or "",
        "workspace_fingerprint": _workspace_fingerprint(files),
        "generated_at": utc_now(),
        "flavors": flavors,
        "profiles": profiles,
        "tools": {
            "ctags": _tool_status("ctags"),
            "cscope": _tool_status("cscope"),
            "global": _tool_status("global"),
        },
        "limits": collection,
        "repos": collection.get("repos") or [],
        "files": files,
        "packages": sections["packages"],
        "symbols": sections["symbols"],
        "build": sections["build"],
        "configs": sections["configs"],
        "device_tree": sections["device_tree"],
        "tests": sections["tests"],
        "entry_points": sections["entry_points"],
        "extractors": extractor_result["extractors"],
        "errors": extractor_result["errors"],
    }


def _annotate_section_repos(sections: dict[str, list[dict]], files: list[dict]) -> None:
    file_repos = {
        str(item.get("path") or ""): (str(item.get("repo") or ROOT_REPO_ID), str(item.get("repo_path") or ""))
        for item in files
        if item.get("path")
    }
    for records in sections.values():
        for record in records:
            path = str(record.get("path") or "")
            repo_id, repo_path = file_repos.get(path, (ROOT_REPO_ID, ""))
            record.setdefault("repo", repo_id)
            record.setdefault("repo_path", repo_path)


def render_project_context_summary(index: dict) -> str:
    counts = _counts(index)
    lines = [
        "# Project Context Index",
        "",
        f"- project_key: `{index.get('project_key') or '-'}`",
        f"- workspace: `{index.get('workspace') or '-'}`",
        f"- workspace_id: `{index.get('workspace_id') or '-'}`",
        f"- git_head: `{index.get('git_head') or '-'}`",
        f"- generated_at: `{index.get('generated_at') or '-'}`",
        f"- flavors: {', '.join(index.get('flavors') or []) or '-'}",
        f"- profiles: {', '.join(index.get('profiles') or []) or '-'}",
        f"- storage: `{_storage_format(index)}`",
        f"- files: {counts['files']}",
        "",
        "## File Kinds",
    ]
    for kind, count in sorted(counts["by_kind"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {kind}: {count}")
    lines.extend(["", "## Tools"])
    tools = index.get("tools") if isinstance(index.get("tools"), dict) else {}
    for name, status in sorted(tools.items()):
        if isinstance(status, dict):
            lines.append(f"- {name}: {'available' if status.get('available') else 'missing'}")
    extractors = index.get("extractors") if isinstance(index.get("extractors"), list) else []
    if extractors:
        lines.extend(["", "## Extractors"])
        for item in extractors:
            lines.append(f"- {item.get('name')}: {item.get('status')} ({item.get('records', 0)} records)")
    errors = index.get("errors") if isinstance(index.get("errors"), list) else []
    if errors:
        lines.extend(["", "## Errors"])
        for item in errors:
            lines.append(f"- {item.get('extractor')}: {item.get('error')}")
    return "\n".join(lines).strip() + "\n"


def _storage_format(index: dict) -> str:
    storage = index.get("storage") if isinstance(index.get("storage"), dict) else {}
    return str(storage.get("format") or "inline-json")


def _write_jsonl_records(path: Path, records: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _section_records(index: dict, section: str) -> list[dict]:
    records = index.get(section)
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _manifest_for_shards(root_dir: Path, index: dict) -> dict:
    manifest = {key: value for key, value in index.items() if key not in RECORD_SECTIONS}
    manifest["counts"] = _counts(index)
    storage: dict = {
        "format": SHARD_STORAGE_FORMAT,
        "version": SHARD_STORAGE_VERSION,
        "sections": {},
    }
    repos_dir = root_dir / REPOS_DIR
    if repos_dir.exists():
        shutil.rmtree(repos_dir)
    for section in RECORD_SECTIONS:
        grouped: dict[str, list[dict]] = {}
        for record in _section_records(index, section):
            repo_id = str(record.get("repo") or ROOT_REPO_ID)
            grouped.setdefault(repo_id, []).append(record)
        shards: list[dict] = []
        for repo_id, records in sorted(grouped.items()):
            relpath = Path(REPOS_DIR) / repo_id / f"{section}.jsonl"
            shard_path = root_dir / relpath
            size = _write_jsonl_records(shard_path, records)
            shards.append(
                {
                    "repo_id": repo_id,
                    "path": relpath.as_posix(),
                    "records": len(records),
                    "bytes": size,
                }
            )
        storage["sections"][section] = {
            "records": sum(item["records"] for item in shards),
            "shards": shards,
        }
    manifest["storage"] = storage
    return manifest


def _load_sharded_records(root_dir: Path, manifest: dict, section: str) -> list[dict]:
    storage = manifest.get("storage") if isinstance(manifest.get("storage"), dict) else {}
    sections = storage.get("sections") if isinstance(storage.get("sections"), dict) else {}
    section_meta = sections.get(section) if isinstance(sections.get(section), dict) else {}
    shards = section_meta.get("shards") if isinstance(section_meta.get("shards"), list) else []
    records: list[dict] = []
    for shard in shards:
        if not isinstance(shard, dict):
            continue
        relpath = str(shard.get("path") or "").strip()
        if not relpath:
            continue
        shard_path = (root_dir / relpath).resolve()
        try:
            shard_path.relative_to(root_dir.resolve())
        except ValueError:
            continue
        loaded, _offset = iter_jsonl_from(shard_path)
        records.extend(record for record in loaded if isinstance(record, dict))
    return records


def _hydrate_sharded_index(root_dir: Path, manifest: dict) -> dict:
    if _storage_format(manifest) != SHARD_STORAGE_FORMAT:
        return manifest
    index = dict(manifest)
    for section in RECORD_SECTIONS:
        index[section] = _load_sharded_records(root_dir, manifest, section)
    return index


def write_project_context_index(root: Path, index: dict) -> dict[str, str]:
    paths = project_context_index_paths(root, str(index["project_key"]), str(index["workspace_id"]))
    paths["dir"].mkdir(parents=True, exist_ok=True)
    manifest = _manifest_for_shards(paths["dir"], index)
    write_json(paths["index"], manifest)
    paths["summary"].parent.mkdir(parents=True, exist_ok=True)
    paths["summary"].write_text(render_project_context_summary(index), encoding="utf-8")
    return {name: str(path) for name, path in paths.items()}


def build_project_context_index(
    root: Path,
    workspace: Path,
    *,
    project_key_value: str | None = None,
    config: dict | None = None,
) -> dict:
    workspace = Path(workspace).expanduser().resolve()
    index = create_project_context_index_document(workspace, project_key_value=project_key_value, config=config)
    paths = write_project_context_index(root, index)
    return {
        "status": "fresh",
        "project_key": index["project_key"],
        "workspace_id": index["workspace_id"],
        "workspace": str(workspace),
        "paths": paths,
        "index": index,
        "counts": _counts(index),
    }


def project_context_index_status(
    root: Path,
    workspace: Path,
    *,
    project_key_value: str | None = None,
    config: dict | None = None,
    verify_worktree: bool = True,
) -> dict:
    workspace = Path(workspace).expanduser().resolve()
    key = project_key_value or derive_project_key(workspace)
    workspace_id = workspace_id_for(workspace)
    paths = project_context_index_paths(root, key, workspace_id)
    git_head = _git_head(workspace)
    git_status_fingerprint = _git_status_fingerprint(workspace) if verify_worktree else ""
    repo_fingerprint = ""
    workspace_fingerprint = ""
    if verify_worktree and git_head:
        repo_fingerprint = _repo_fingerprint(discover_project_repos(workspace))
    if verify_worktree and not git_head:
        files, _collection = collect_project_files(workspace, config)
        workspace_fingerprint = _workspace_fingerprint(files)
    base = {
        "project_key": key,
        "workspace_id": workspace_id,
        "workspace": str(workspace),
        "paths": {name: str(path) for name, path in paths.items()},
        "git_head": git_head,
        "git_status_fingerprint": git_status_fingerprint,
        "repo_fingerprint": repo_fingerprint,
        "workspace_fingerprint": workspace_fingerprint,
        "stale_check": "full" if verify_worktree else "head-only",
        "verified_worktree": bool(verify_worktree),
    }
    if not paths["index"].exists():
        return {**base, "status": "missing", "exists": False, "counts": _counts({})}
    try:
        index = read_json(paths["index"])
    except (OSError, ValueError):
        return {**base, "status": "failed", "exists": True, "error": "index is unreadable", "counts": _counts({})}
    stale = (
        index.get("schema_version") != INDEX_SCHEMA_VERSION
        or bool(base["git_head"] and str(index.get("git_head") or "") != str(base["git_head"]))
        or bool(
            verify_worktree
            and str(index.get("git_status_fingerprint") or "") != str(base["git_status_fingerprint"] or "")
        )
        or bool(
            verify_worktree
            and base["repo_fingerprint"]
            and str(index.get("repo_fingerprint") or "") != str(base["repo_fingerprint"])
        )
        or bool(
            verify_worktree
            and base["workspace_fingerprint"]
            and str(index.get("workspace_fingerprint") or "") != str(base["workspace_fingerprint"])
        )
    )
    return {
        **base,
        "status": "stale" if stale else "fresh",
        "exists": True,
        "schema_version": index.get("schema_version"),
        "generated_at": index.get("generated_at"),
        "flavors": index.get("flavors") or [],
        "profiles": index.get("profiles") or [],
        "tools": index.get("tools") or {},
        "extractors": index.get("extractors") or [],
        "errors": index.get("errors") or [],
        "storage": index.get("storage") or {},
        "repos": index.get("repos") or [],
        "counts": _counts(index),
        "index": index,
    }


def load_project_context_index(
    root: Path,
    workspace: Path,
    *,
    project_key_value: str | None = None,
    config: dict | None = None,
    hydrate: bool = False,
) -> dict | None:
    del config
    workspace = Path(workspace).expanduser().resolve()
    key = project_key_value or derive_project_key(workspace)
    workspace_id = workspace_id_for(workspace)
    path = project_context_index_paths(root, key, workspace_id)["index"]
    if not path.exists():
        return None
    try:
        manifest = read_json(path)
    except (OSError, ValueError):
        return None
    if hydrate:
        return _hydrate_sharded_index(path.parent, manifest)
    return manifest


def _query_terms(text: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[a-zA-Z0-9_./+-]{2,}", text.lower()):
        for part in _tokenize_text(token):
            if part not in terms:
                terms.append(part)
    for run in re.findall(r"[一-鿿]{2,}", text):
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in terms:
                terms.append(bigram)
    return terms


def _tokenize_text(text: str) -> list[str]:
    pieces: list[str] = []
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    for token in re.split(r"[^A-Za-z0-9]+", normalized.lower()):
        if len(token) < 2:
            continue
        pieces.append(token)
    joined = "".join(pieces)
    if len(joined) >= 3 and joined not in pieces:
        pieces.append(joined)
    return pieces


def _trigrams(text: str) -> set[str]:
    text = re.sub(r"[^a-z0-9]+", "", text.lower())
    if len(text) < 3:
        return {text} if text else set()
    return {text[index : index + 3] for index in range(len(text) - 2)}


def _ngram_overlap(left: str, right: str) -> float:
    left_grams = _trigrams(left)
    right_grams = _trigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _record_haystack(item: dict) -> str:
    values: list[str] = []

    def add_value(value) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for nested in value.values():
                add_value(nested)
            return
        if isinstance(value, list):
            for nested in value:
                add_value(nested)
            return
        values.append(str(value))

    for key in (
        "path",
        "kind",
        "ext",
        "name",
        "language",
        "signature",
        "prompt",
        "value",
        "node",
        "op",
        "package_dir",
        "config_path",
        "mk_path",
    ):
        add_value(item.get(key))
    for key in ("depends", "selects", "config_symbols", "dependencies", "enabled_in", "variables"):
        add_value(item.get(key))
    return " ".join(values).lower()


def _record_tokens_from(item: dict, keys: tuple[str, ...]) -> set[str]:
    values: list[str] = []

    def add(value) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for nested in value.values():
                add(nested)
            return
        if isinstance(value, list):
            for nested in value:
                add(nested)
            return
        values.append(str(value))

    for key in keys:
        add(item.get(key))
    tokens: set[str] = set()
    for value in values:
        tokens.update(_tokenize_text(value))
    return tokens


def _record_token_groups(item: dict) -> tuple[set[str], set[str], set[str]]:
    strong = _record_tokens_from(
        item,
        (
            "path",
            "name",
            "kind",
            "package_dir",
            "config_path",
            "mk_path",
            "config_symbols",
            "node",
            "signature",
        ),
    )
    medium = _record_tokens_from(item, ("language", "dependencies", "depends", "selects", "op"))
    weak = _record_tokens_from(item, ("enabled_in", "variables", "value", "prompt", "ext"))
    return strong, medium, weak


def _record_tokens(item: dict) -> set[str]:
    strong, medium, weak = _record_token_groups(item)
    return strong | medium | weak


def _term_token_score(term: str, tokens: set[str], *, exact: int, partial: int, fuzzy: int) -> int:
    if term in tokens:
        return exact
    if any(term in token or token in term for token in tokens if len(token) >= 3):
        return partial
    if len(term) >= 4 and any(
        abs(len(term) - len(token)) <= 4 and _ngram_overlap(term, token) >= 0.62
        for token in tokens
        if 3 <= len(token) <= 40
    ):
        return fuzzy
    return 0


def _record_score(item: dict, terms: list[str]) -> int:
    strong_tokens, medium_tokens, weak_tokens = _record_token_groups(item)
    score = 0
    matched_terms = 0
    name = str(item.get("name") or "").lower()
    compact_name = re.sub(r"[^a-z0-9]+", "", name)
    path = str(item.get("path") or "").lower()
    path_segments = {segment for segment in re.split(r"[/._+-]+", path) if segment}
    for term in terms:
        term_score = 0
        if term == name:
            term_score += 20
        elif term and term == compact_name:
            term_score += 20
        elif term and term in name:
            term_score += 8
        elif len(term) >= 4 and term in compact_name:
            term_score += 6
        if term in path_segments:
            term_score += 6
        elif any(segment.startswith(term) or term.startswith(segment) for segment in path_segments if len(segment) >= 3):
            term_score += 2
        term_score += _term_token_score(term, strong_tokens, exact=8, partial=3, fuzzy=2)
        term_score += _term_token_score(term, medium_tokens, exact=4, partial=2, fuzzy=1)
        if term in weak_tokens:
            term_score += 1
        if term_score > 0:
            matched_terms += 1
            score += term_score
    if matched_terms <= 0:
        return 0
    if len(terms) > 1:
        score += matched_terms * 4
        if matched_terms >= min(len(terms), 2):
            score += 8
    return score


def _record_primary_path(record: dict) -> str:
    return str(record.get("path") or record.get("mk_path") or record.get("config_path") or "")


def _rank_hint_paths(rank_hints: dict | None) -> list[str]:
    if not isinstance(rank_hints, dict):
        return []
    hints = rank_hints.get("path_hints")
    if not isinstance(hints, list):
        return []
    out: list[str] = []
    for hint in hints:
        value = str(hint or "").strip().replace("\\", "/").strip("/")
        if value and value not in out:
            out.append(value)
    return out[:24]


def _record_path_hint_score(record: dict, rank_hints: dict | None) -> int:
    hints = _rank_hint_paths(rank_hints)
    if not hints:
        return 0
    path = _record_primary_path(record).replace("\\", "/").strip("/")
    if not path:
        return 0
    score = 0
    for hint in hints:
        if path == hint:
            score += 60
        elif path.startswith(hint.rstrip("/") + "/"):
            score += 32
        elif hint in path:
            score += 18
        elif path in hint:
            score += 24
    return score


def _ranked_sort_key(item: tuple[int, dict], profiles: list[str] | tuple[str, ...] | None = None) -> tuple[int, int, str]:
    score, record = item
    path = _record_primary_path(record)
    return (-score, _path_priority(path, profiles), path)


def _rank_records(
    records: list[dict],
    terms: list[str],
    *,
    limit: int,
    profiles: list[str] | tuple[str, ...] | None = None,
    rank_hints: dict | None = None,
) -> tuple[list[dict], int]:
    ranked: list[tuple[int, dict]] = []
    for item in records:
        score = _record_score(item, terms) + _record_path_hint_score(item, rank_hints)
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda item: _ranked_sort_key(item, profiles))
    return [item for _score, item in ranked[: max(0, limit)]], len(ranked)


def _iter_jsonl_records(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _iter_candidate_jsonl_records(path: Path, terms: list[str]):
    if not path.exists():
        return
    useful_terms = [term for term in terms if len(term) >= 3]
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            if useful_terms:
                lowered = stripped.lower()
                if not any(term in lowered for term in useful_terms):
                    compact = re.sub(r"[^a-z0-9]+", "", lowered)
                    if not any(term in compact for term in useful_terms):
                        continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _iter_sharded_section_records(root_dir: Path, manifest: dict, section: str):
    storage = manifest.get("storage") if isinstance(manifest.get("storage"), dict) else {}
    sections = storage.get("sections") if isinstance(storage.get("sections"), dict) else {}
    section_meta = sections.get(section) if isinstance(sections.get(section), dict) else {}
    shards = section_meta.get("shards") if isinstance(section_meta.get("shards"), list) else []
    resolved_root = root_dir.resolve()
    for shard in shards:
        if not isinstance(shard, dict):
            continue
        relpath = str(shard.get("path") or "").strip()
        if not relpath:
            continue
        shard_path = (root_dir / relpath).resolve()
        try:
            shard_path.relative_to(resolved_root)
        except ValueError:
            continue
        yield from _iter_jsonl_records(shard_path) or ()


def _iter_sharded_section_candidates(root_dir: Path, manifest: dict, section: str, terms: list[str]):
    storage = manifest.get("storage") if isinstance(manifest.get("storage"), dict) else {}
    sections = storage.get("sections") if isinstance(storage.get("sections"), dict) else {}
    section_meta = sections.get(section) if isinstance(sections.get(section), dict) else {}
    shards = section_meta.get("shards") if isinstance(section_meta.get("shards"), list) else []
    resolved_root = root_dir.resolve()
    for shard in shards:
        if not isinstance(shard, dict):
            continue
        relpath = str(shard.get("path") or "").strip()
        if not relpath:
            continue
        shard_path = (root_dir / relpath).resolve()
        try:
            shard_path.relative_to(resolved_root)
        except ValueError:
            continue
        yield from _iter_candidate_jsonl_records(shard_path, terms) or ()


def _rank_sharded_section(
    root_dir: Path,
    manifest: dict,
    section: str,
    terms: list[str],
    *,
    limit: int,
    profiles: list[str] | tuple[str, ...] | None = None,
    rank_hints: dict | None = None,
) -> tuple[list[dict], int]:
    ranked: list[tuple[int, dict]] = []
    total = 0
    keep = max(0, limit)
    trim_threshold = max(keep * 8, 128)
    for record in _iter_sharded_section_candidates(root_dir, manifest, section, terms) or ():
        score = _record_score(record, terms) + _record_path_hint_score(record, rank_hints)
        if score <= 0:
            continue
        total += 1
        if keep <= 0:
            continue
        ranked.append((score, record))
        if len(ranked) > trim_threshold:
            ranked.sort(key=lambda item: _ranked_sort_key(item, profiles))
            del ranked[keep:]
    ranked.sort(key=lambda item: _ranked_sort_key(item, profiles))
    return [record for _score, record in ranked[:keep]], total


def _record_owner_paths(record: dict) -> list[str]:
    paths: list[str] = []
    for key in ("path", "mk_path", "config_path"):
        value = str(record.get(key) or "").strip()
        if value and value not in paths:
            paths.append(value)
    return paths


def _owner_paths_from_sections(section_matches: dict[str, list[dict]]) -> list[str]:
    paths: list[str] = []
    for section in ("symbols", "configs", "build", "device_tree", "entry_points", "packages"):
        for record in section_matches.get(section) or []:
            for path in _record_owner_paths(record):
                if path not in paths:
                    paths.append(path)
    return paths[:24]


def _promote_owner_files(
    matched_files: list[dict],
    owner_files: list[dict],
    *,
    limit: int,
) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for record in [*owner_files, *matched_files]:
        path = str(record.get("path") or "").strip()
        if not path or path in seen:
            continue
        merged.append(record)
        seen.add(path)
        if len(merged) >= max(0, limit):
            break
    return merged


def _file_records_by_owner_paths(files: list[dict], owner_paths: list[str]) -> list[dict]:
    wanted = set(owner_paths)
    found: dict[str, dict] = {}
    for record in files:
        path = str(record.get("path") or "").strip()
        if path in wanted and path not in found:
            found[path] = record
    return [found[path] for path in owner_paths if path in found]


def _sharded_file_records_by_owner_paths(root_dir: Path, manifest: dict, owner_paths: list[str]) -> list[dict]:
    wanted = set(owner_paths)
    found: dict[str, dict] = {}
    for record in _iter_sharded_section_records(root_dir, manifest, "files") or ():
        path = str(record.get("path") or "").strip()
        if path in wanted and path not in found:
            found[path] = record
            if len(found) >= len(wanted):
                break
    return [found[path] for path in owner_paths if path in found]


def query_project_context_index(
    index: dict,
    query: str,
    *,
    max_files: int = 8,
    max_records: int = 8,
    rank_hints: dict | None = None,
) -> dict:
    terms = _query_terms(query)
    profiles = index.get("profiles") if isinstance(index.get("profiles"), list) else []
    files = index.get("files") if isinstance(index.get("files"), list) else []
    matched_files, total_files = _rank_records(files, terms, limit=max_files, profiles=profiles, rank_hints=rank_hints)
    section_matches: dict[str, list[dict]] = {}
    section_totals: dict[str, int] = {"files": total_files}
    for section in ("packages", "symbols", "configs", "build", "device_tree", "tests", "entry_points"):
        records = index.get(section) if isinstance(index.get(section), list) else []
        matched, total = _rank_records(records, terms, limit=max_records, profiles=profiles, rank_hints=rank_hints)
        section_matches[section] = matched
        section_totals[section] = total
    owner_paths = _owner_paths_from_sections(section_matches)
    owner_files = _file_records_by_owner_paths(files, owner_paths)
    matched_files = _promote_owner_files(matched_files, owner_files, limit=max_files)
    return {
        "query": query,
        "terms": terms,
        "files": matched_files,
        **section_matches,
        "section_totals": section_totals,
        "total_matches": sum(section_totals.values()),
    }


def _query_project_context_index_manifest(
    root_dir: Path,
    manifest: dict,
    query: str,
    *,
    max_files: int = 8,
    max_records: int = 8,
    rank_hints: dict | None = None,
) -> dict:
    if _storage_format(manifest) != SHARD_STORAGE_FORMAT:
        return query_project_context_index(
            manifest,
            query,
            max_files=max_files,
            max_records=max_records,
            rank_hints=rank_hints,
        )
    terms = _query_terms(query)
    profiles = manifest.get("profiles") if isinstance(manifest.get("profiles"), list) else []
    matched_files, total_files = _rank_sharded_section(
        root_dir,
        manifest,
        "files",
        terms,
        limit=max_files,
        profiles=profiles,
        rank_hints=rank_hints,
    )
    section_matches: dict[str, list[dict]] = {}
    section_totals: dict[str, int] = {"files": total_files}
    for section in ("packages", "symbols", "configs", "build", "device_tree", "tests", "entry_points"):
        matched, total = _rank_sharded_section(
            root_dir,
            manifest,
            section,
            terms,
            limit=max_records,
            profiles=profiles,
            rank_hints=rank_hints,
        )
        section_matches[section] = matched
        section_totals[section] = total
    owner_paths = _owner_paths_from_sections(section_matches)
    owner_files = _sharded_file_records_by_owner_paths(root_dir, manifest, owner_paths)
    matched_files = _promote_owner_files(matched_files, owner_files, limit=max_files)
    return {
        "query": query,
        "terms": terms,
        "files": matched_files,
        **section_matches,
        "section_totals": section_totals,
        "total_matches": sum(section_totals.values()),
    }


def query_project_context_index_file(
    index_path: Path,
    query: str,
    *,
    max_files: int = 8,
    max_records: int = 8,
    rank_hints: dict | None = None,
) -> dict | None:
    index_path = Path(index_path).expanduser().resolve()
    if not index_path.exists():
        return None
    try:
        manifest = read_json(index_path)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    return _query_project_context_index_manifest(
        index_path.parent,
        manifest,
        query,
        max_files=max_files,
        max_records=max_records,
        rank_hints=rank_hints,
    )


def query_project_context_index_cache(
    root: Path,
    workspace: Path,
    query: str,
    *,
    project_key_value: str | None = None,
    config: dict | None = None,
    max_files: int = 8,
    max_records: int = 8,
) -> dict | None:
    workspace = Path(workspace).expanduser().resolve()
    key = project_key_value or derive_project_key(workspace)
    workspace_id = workspace_id_for(workspace)
    index_path = project_context_index_paths(root, key, workspace_id)["index"]
    resolved_query = str(query or "")
    resolution: dict | None = None
    try:
        from aha_cli.services.project_context_resolver import resolve_project_context_query

        resolution = resolve_project_context_query(
            root,
            workspace,
            resolved_query,
            config=config,
            project_key_value=key,
        )
        resolved_query = str(resolution.get("query") or resolved_query)
    except (Exception, SystemExit):
        resolution = None
    result = query_project_context_index_file(
        index_path,
        resolved_query,
        max_files=max_files,
        max_records=max_records,
        rank_hints=resolution,
    )
    if result is not None and resolution and resolution.get("used_navigation"):
        result["query"] = query
        result["resolved_query"] = resolved_query
        result["resolution"] = resolution
    return result


def _record_location(record: dict) -> str:
    path = str(record.get("path") or record.get("mk_path") or record.get("config_path") or "")
    line = record.get("line")
    return f"{path}:{line}" if path and line else path


def _append_reference_line(lines: list[str], line: str, budget_chars: int) -> bool:
    current = sum(len(item) + 1 for item in lines)
    if current + len(line) + 1 > budget_chars:
        return False
    lines.append(line)
    return True


def _drop_trailing_reference_header(lines: list[str]) -> None:
    while lines and lines[-1].startswith("- ") and lines[-1].endswith(":"):
        lines.pop()


def format_project_context_reference(result: dict, *, budget_chars: int = 1200) -> str:
    query = str(result.get("query") or "-")
    if not int(result.get("total_matches") or 0):
        return ""
    lines = [
        "Project map reference:",
        f"- query: {query}",
    ]
    resolution = result.get("resolution") if isinstance(result.get("resolution"), dict) else {}
    if resolution.get("used_navigation"):
        routes = resolution.get("nav_routes") if isinstance(resolution.get("nav_routes"), list) else []
        route_text = " -> ".join(
            str(item.get("slug") or item.get("title") or "-")
            for item in routes[:3]
            if isinstance(item, dict)
        )
        if route_text:
            _append_reference_line(lines, f"- nav route: {route_text}", budget_chars)
    packages = result.get("packages") if isinstance(result.get("packages"), list) else []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    has_location = bool(files) or any(_record_location(item) for item in packages)
    if packages:
        _append_reference_line(lines, "- packages:", budget_chars)
        for item in packages[:4]:
            deps = item.get("dependencies") if isinstance(item.get("dependencies"), list) else []
            enabled = item.get("enabled_in") if isinstance(item.get("enabled_in"), list) else []
            parts = [f"  - {item.get('name') or '-'}"]
            if item.get("mk_path"):
                parts.append(f"mk={item.get('mk_path')}")
            if item.get("config_path"):
                parts.append(f"config={item.get('config_path')}")
            if deps:
                parts.append(f"deps={','.join(str(dep) for dep in deps[:5])}")
            if enabled:
                parts.append(f"enabled={len(enabled)}")
            if not _append_reference_line(lines, "; ".join(parts), budget_chars):
                break
    if files:
        _append_reference_line(lines, "- files:", budget_chars)
        for item in files[:6]:
            if not _append_reference_line(lines, f"  - {item.get('path')} ({item.get('kind') or 'file'})", budget_chars):
                break
    for section, label in (
        ("symbols", "symbols"),
        ("configs", "configs"),
        ("build", "build"),
        ("device_tree", "device-tree"),
        ("entry_points", "entry-points"),
    ):
        records = result.get(section) if isinstance(result.get(section), list) else []
        if not records:
            continue
        has_location = has_location or any(_record_location(item) for item in records)
        if not _append_reference_line(lines, f"- {label}:", budget_chars):
            break
        for item in records[:6]:
            name = item.get("name") or item.get("value") or item.get("node") or "-"
            location = _record_location(item)
            detail = item.get("kind") or section
            text = f"  - {name} ({detail})"
            if location:
                text += f" {location}"
            if not _append_reference_line(lines, text, budget_chars):
                break
    final_line = "Read exact files by path before editing."
    if has_location and final_line not in lines:
        _drop_trailing_reference_header(lines)
        while lines and not _append_reference_line(lines, final_line, budget_chars):
            lines.pop()
            _drop_trailing_reference_header(lines)
    return "\n".join(lines)


__all__ = [
    "ProjectContextExtractor",
    "build_project_context_index",
    "collect_project_files",
    "create_project_context_index_document",
    "default_project_context_extractors",
    "detect_project_flavors",
    "discover_project_repos",
    "format_project_context_reference",
    "load_project_context_index",
    "project_context_index_config",
    "project_context_index_dir",
    "project_context_index_paths",
    "project_context_index_status",
    "query_project_context_index",
    "query_project_context_index_cache",
    "query_project_context_index_file",
    "render_project_context_summary",
    "run_project_context_extractors",
    "write_project_context_index",
    "workspace_id_for",
]
