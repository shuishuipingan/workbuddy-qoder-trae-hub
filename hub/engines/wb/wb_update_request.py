# -*- coding: utf-8 -*-
"""Panel-side request/status bridge for the privileged update service.

The gateway process stays unprivileged. It can create one validated request in
a root-owned sticky directory and read status files written by the updater.
It never runs systemctl or writes application files itself.
"""

import json
import os
import re
import shutil
import stat
import time

import wb_update_portable


REQUEST_DIR = os.environ.get(
    "WB_UPDATE_REQUEST_DIR", "/run/workbuddy2api-hub-updater")
REQUEST_FILE = os.path.join(REQUEST_DIR, "request.json")
STATUS_FILE = os.path.join(REQUEST_DIR, "status.json")
#: Inventory the root updater keeps next to status.json, so the panel can offer
#: the backups that live under its root-only backup directory.
BACKUPS_FILE = os.path.join(REQUEST_DIR, "backups.json")
PATH_UNIT_FILE = "/etc/systemd/system/workbuddy2api-hub-updater.path"
ENABLED_UNIT_LINK = ("/etc/systemd/system/multi-user.target.wants/"
                     "workbuddy2api-hub-updater.path")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ACTIVE_STATES = frozenset((
    "queued", "downloading", "verifying", "backing_up", "stopping",
    "applying", "restoring", "restarting", "checking", "rolling_back",
))
VISIBLE_STATES = frozenset((
    "idle", "queued", "downloading", "verifying", "backing_up",
    "stopping", "applying", "restoring", "restarting", "checking", "succeeded",
    "restored", "rolling_back", "rolled_back", "failed",
))
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class UpdateRequestError(Exception):
    """A safe, user-visible reason an install request was refused."""


def _systemd_enabled():
    """Only enable the systemd path after the administrator installs its unit."""
    return (
        str(os.environ.get("WB_UPDATE_INSTALL_ENABLED") or "").strip() == "1"
        and os.path.isdir(REQUEST_DIR)
        and os.path.exists(PATH_UNIT_FILE)
        and os.path.exists(ENABLED_UNIT_LINK)
    )


def external_enabled():
    """A deployment-provided executor polls request.json (issue #561).

    The gateway still only writes a validated request; the executor is the
    deployment's supervisor/entrypoint. Registration is either the
    WB_UPDATE_EXECUTOR=external environment variable or an executor.json
    marker in the request directory.
    """
    if str(os.environ.get("WB_UPDATE_INSTALL_ENABLED") or "").strip() != "1":
        return False
    if not os.path.isdir(REQUEST_DIR):
        return False
    mode = str(os.environ.get("WB_UPDATE_EXECUTOR") or "").strip().lower()
    return (mode == "external"
            or os.path.exists(os.path.join(REQUEST_DIR, "executor.json")))


def install_enabled():
    """The panel may install when either the unit or a portable package is here."""
    return (_systemd_enabled() or external_enabled()
            or wb_update_portable.available())


def _version_tuple(tag):
    match = TAG_RE.fullmatch(str(tag or "").strip())
    return tuple(int(value) for value in match.groups()) if match else None


def _read_root_status():
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW
    try:
        fd = os.open(STATUS_FILE, flags)
    except FileNotFoundError:
        return {"state": "idle", "version": "", "message": ""}
    except OSError:
        return {"state": "unavailable", "version": "", "message": ""}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_uid", -1) != 0
                or info.st_size > 8192 or (stat.S_IMODE(info.st_mode) & 0o022)):
            return {"state": "unavailable", "version": "", "message": ""}
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = json.loads(stream.read(8193).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {"state": "unavailable", "version": "", "message": ""}
    finally:
        os.close(fd)

    if not isinstance(payload, dict):
        return {"state": "unavailable", "version": "", "message": ""}
    state = str(payload.get("state") or "")
    if state not in VISIBLE_STATES:
        return {"state": "unavailable", "version": "", "message": ""}
    version = str(payload.get("version") or "")[:32]
    message = str(payload.get("message") or "")[:240]
    updated_at = str(payload.get("updated_at") or "")[:32]
    result = {"state": state, "version": version, "message": message}
    if updated_at:
        result["updated_at"] = updated_at
    return result


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _root_backups():
    """The root updater's backup inventory (systemd layout), or [] when absent.

    The folders themselves sit under a root-only backup directory, so the
    gateway cannot list them; the updater publishes this file next to
    status.json instead, and it gets the same ownership and mode checks as the
    status file before anything in it is believed.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW
    try:
        fd = os.open(BACKUPS_FILE, flags)
    except OSError:
        return []
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_uid", -1) != 0
                or info.st_size > 65536 or (stat.S_IMODE(info.st_mode) & 0o022)):
            return []
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = json.loads(stream.read(65537).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return []
    finally:
        os.close(fd)
    items = payload.get("backups") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version") or "")
        if not VERSION_RE.fullmatch(version):
            continue
        out.append({
            "version": version,
            "created_at": str(item.get("created_at") or "")[:32],
            "files": _int(item.get("files")),
            "complete": item.get("complete") is True,
            "reason": str(item.get("reason") or "")[:120],
            "size_mb": _int(item.get("size_mb")),
            "source": "systemd",
        })
    return out


def backups():
    """Retained backups the panel may offer to restore, newest first.

    Which executor is registered decides where they come from: the portable and
    external executors keep them inside the application directory (readable by
    the gateway), the systemd worker keeps them under /var/backups and publishes
    an inventory instead.
    """
    if _systemd_enabled():
        return {"source": "systemd", "backups": _root_backups()}
    if external_enabled() or wb_update_portable.available():
        root = os.path.dirname(os.path.abspath(__file__))
        entries = wb_update_portable.list_backups(root)
        for entry in entries:
            entry["source"] = "local"
        return {"source": "local", "backups": entries}
    return {"source": "", "backups": []}


def apply_keep_backups(keep):
    """Prune retained backups down to `keep` right after the setting changed.

    Returns the folder versions that were removed. A deployment whose backups
    live outside the application directory (the systemd worker) prunes them on
    its next run instead, because the gateway cannot delete root-owned files.

    The transient copy a failed rollback left behind goes too: it is never a
    rollback entry, and a setting that says "keep nothing" must not leave a
    full copy of the app behind.
    """
    if _systemd_enabled():
        return []
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        shutil.rmtree(os.path.join(wb_update_portable.backups_dir(root),
                                   wb_update_portable.SAFETY_DIR),
                      ignore_errors=True)
    except OSError:
        pass
    try:
        return wb_update_portable.prune_backups(root, keep)
    except OSError:
        return []


def status():
    if _systemd_enabled() or external_enabled():
        return _bridge_status()
    if wb_update_portable.available():
        return {"install_enabled": True,
                "installation": wb_update_portable.status()}
    return {"install_enabled": False,
            "installation": {"state": "idle", "version": "", "message": ""}}


def _bridge_status():
    result = {"install_enabled": True, "installation": _read_root_status()}
    current = result["installation"]
    if (os.path.exists(REQUEST_FILE)
            and current.get("state") not in ACTIVE_STATES):
        result["installation"] = {
            "state": "queued", "version": current.get("version", ""),
            "message": "",
        }
    return result


def _request_fields(version, port):
    """The parts every request carries: which process to stop, where it runs."""
    fields = {
        "version": version,
        "requested_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        # The external executor needs to know which process to stop and
        # which directory to replace; the systemd worker ignores extras.
        "pid": os.getpid(),
        "port": int(port),
        "root": os.path.dirname(os.path.abspath(__file__)),
    }
    # 镜像 / 自建 rootfs 部署只拥有应用层（wb_*.py + dashboard.html）。把档位随
    # 请求带下去，执行者就不会再写进 start-wb-proxy*、ops/ 和 pricing/pricing.json
    # —— 后者一旦落盘就会盖掉 wb_pricing.py 的内联快照，而且两份没有同步机制
    # （#573）。便携包照旧按整份清单更新。
    try:
        # 便携包的清单在包根、代码在 `wb-proxy/` 子目录；镜像 / rootfs 部署的
        # 清单（如果有）和代码在同一层。用这个结构差异判断，而不是看清单文件
        # 是否存在——旧执行者会把清单写进 /app，之后再判断就会误认成便携包。
        if (wb_update_portable.package_root()
                == wb_update_portable.app_root()):
            fields["profile"] = "app"
    except Exception:
        pass
    return fields


def _queue_request(fields):
    """Create the one request file the executor consumes; caller holds the lock."""
    blob = json.dumps(fields, separators=(",", ":")).encode("utf-8")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW)
    try:
        fd = os.open(REQUEST_FILE, flags, 0o600)
    except FileExistsError:
        raise UpdateRequestError("已有更新任务正在运行")
    except OSError:
        raise UpdateRequestError("无法提交更新任务，请检查更新服务状态")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            os.unlink(REQUEST_FILE)
        except OSError:
            pass
        raise UpdateRequestError("提交更新任务失败")


def request_install(tag, current_version, port=8788, lan=False, repo=None):
    """Queue a stable tag for the root updater; caller must panel-authenticate."""
    if not install_enabled():
        raise UpdateRequestError("安装更新服务尚未配置")
    target = _version_tuple(tag)
    current = _version_tuple("v" + str(current_version or "").strip().lstrip("v"))
    if target is None or current is None or target <= current:
        raise UpdateRequestError("目标版本无效或不高于当前版本")

    if not _systemd_enabled() and not external_enabled():
        try:
            return wb_update_portable.request_install(
                tag, current_version, port=port, lan=lan, repo=repo)
        except wb_update_portable.UpdateError as exc:
            raise UpdateRequestError(str(exc))

    existing = status()["installation"]
    if existing.get("state") in ACTIVE_STATES or os.path.exists(REQUEST_FILE):
        raise UpdateRequestError("已有更新任务正在运行")

    version = "%d.%d.%d" % target
    fields = _request_fields(version, port)
    fields["action"] = "install"
    fields["tag"] = tag
    _queue_request(fields)
    return {"state": "queued", "version": version,
            "message": "更新任务已排队"}


def request_rollback(version, current_version, port=8788, lan=False):
    """Queue a restore of a retained backup; caller must panel-authenticate.

    The target is checked against the backups this deployment actually has, so
    an arbitrary version can never reach the executor. The request still carries
    `tag: "v<version>"`: an executor that predates this feature ignores `action`
    and treats the request as "reinstall that tag", which reaches the same
    version at the cost of a download instead of failing outright.
    """
    if not install_enabled():
        raise UpdateRequestError("安装更新服务尚未配置")
    target = str(version or "").strip().lstrip("v")
    match = VERSION_RE.fullmatch(target)
    if not match:
        raise UpdateRequestError("回退目标版本无效")
    target = "%d.%d.%d" % tuple(int(part) for part in match.groups())
    current = str(current_version or "").strip().lstrip("v")
    if target == current:
        raise UpdateRequestError("回退目标与当前版本相同")
    if not any(entry.get("complete") and entry.get("version") == target
               for entry in backups()["backups"]):
        raise UpdateRequestError("没有可回退到 %s 的完整备份" % target)

    if not _systemd_enabled() and not external_enabled():
        try:
            return wb_update_portable.request_rollback(
                target, current_version, port=port, lan=lan)
        except wb_update_portable.UpdateError as exc:
            raise UpdateRequestError(str(exc))

    existing = status()["installation"]
    if existing.get("state") in ACTIVE_STATES or os.path.exists(REQUEST_FILE):
        raise UpdateRequestError("已有更新任务正在运行")

    fields = _request_fields(target, port)
    fields["action"] = "rollback"
    fields["tag"] = "v" + target
    _queue_request(fields)
    return {"state": "queued", "version": target, "message": "回退任务已排队"}
