"""Central log paths, conservative legacy usage migration, and stream tee."""
import os
import shutil
import sys
import threading
from pathlib import Path


def resolve_log_root(app_root):
    return str(Path(os.environ.get("QD_LOG_DIR") or Path(app_root)/"logs").resolve())


def resolve_usage_dir(app_root):
    explicit = os.environ.get("QD_PROXY_USAGE_DIR")
    return str(Path(explicit).resolve()) if explicit else str(Path(resolve_log_root(app_root))/"usage")


def migrate_legacy_usage(app_root, destination=None):
    """Copy old data before first new start; never overwrite or delete originals.

    Existing destination wins. If destination has any file while source contains
    missing names, report conflicts instead of merging unrelated history.
    """
    old = Path(app_root)/"usage"
    new = Path(destination or resolve_usage_dir(app_root))
    if not old.is_dir() or old.resolve() == new.resolve():
        return {"copied": [], "conflicts": []}
    candidates = [p for p in old.iterdir() if p.is_file() and p.suffix in (".jsonl", ".json")]
    copied, conflicts = [], []
    populated = new.is_dir() and any(new.iterdir())
    for source in candidates:
        target = new/source.name
        if target.exists() or populated:
            conflicts.append(source.name)
            continue
        new.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix+".migrate.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            copied.append(source.name)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {"copied": copied, "conflicts": conflicts}


class _LogTee:
    def __init__(self, original, path):
        self.original = original
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, text):
        with self.lock:
            try:
                self.original.write(text)
            except (OSError, UnicodeError):
                pass
            self.file.write(text)
        return len(text)

    def flush(self):
        with self.lock:
            try:
                self.original.flush()
            except OSError:
                pass
            self.file.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self.original.fileno()


def install_stream_logging(app_root):
    """Only call at server startup, not module import or unit-test bootstrap.

    Launchers may already redirect to these files; QD_LOG_REDIRECTED avoids
    duplicate writes. Direct terminal/container starts retain console output.
    """
    root = Path(resolve_log_root(app_root))
    root.mkdir(parents=True, exist_ok=True)
    if os.environ.get("QD_LOG_REDIRECTED") != "1":
        if not isinstance(sys.stdout, _LogTee):
            sys.stdout = _LogTee(sys.stdout, root/"gateway.log")
        if not isinstance(sys.stderr, _LogTee):
            sys.stderr = _LogTee(sys.stderr, root/"gateway.err.log")
    return str(root)
