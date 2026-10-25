# -*- coding: utf-8 -*-
"""updater —— 版本检查与一键更新。

口径（和引擎里的 update_check_enabled 无关）：
  · 检查：向 GitHub Releases 取 latest，与 hub.HUB_VERSION 比较；结果缓存 6 小时，
    面板打开不会每次都打 GitHub（公开仓库、只读、无需 token）。
  · 更新：只接受本仓库产出的 release zip；解压覆盖代码，但**绝不覆盖数据** ——
    data/、hub/settings.json、engines/*/accounts、engines/*/usage、logs/ 全部跳过。
  · 重启：更新后需重启才生效；调用方决定是"提示重启"还是"自动重启"。

失败一律返回结构化原因（不抛给 HTTP 层），面板据此显示人话。
"""
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from hub import HUB_VERSION

REPO = os.environ.get("HUB_UPDATE_REPO") or "shuishuipingan/workbuddy-qoder-trae-hub"
API = "https://api.github.com"
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
STATE_DIR = os.path.join(ROOT, "state")
CACHE_FILE = os.path.join(STATE_DIR, "update.json")
CACHE_TTL = 6 * 3600

# 这些路径**永不覆盖**：账号、用量、日志、面板设置、状态目录
PROTECTED = (
    ".git/", "data/", "logs/", "state/",
    "hub/settings.json", "hub/route-settings.json",
    "hub/engines/wb/accounts/", "hub/engines/wb/usage/",
    "hub/engines/qd/accounts/", "hub/engines/qd/usage/",
    "hub/engines/trae/accounts/", "hub/engines/trae/usage/",
)


def version_tuple(text):
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _ensure_state():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass


def _read_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(doc):
    _ensure_state()
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _get_json(url, timeout=8.0):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "hub-updater/" + HUB_VERSION,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check(force=False):
    """返回 {current, latest, update_available, url, notes, checked_at, error}。"""
    now = time.time()
    cache = _read_cache()
    if not force and cache.get("checked_at") and now - float(cache["checked_at"]) < CACHE_TTL:
        # 缓存里存的是"上次检查时"的 current；升级后进程里的 HUB_VERSION 已经变了，
        # 直接回旧值会让面板一直显示老版本号。current 必须每次用实时值覆盖。
        cache["cached"] = True
        cache["current"] = HUB_VERSION
        cache["update_available"] = version_tuple(cache.get("latest")) > version_tuple(HUB_VERSION)
        return cache
    out = {"current": HUB_VERSION, "latest": HUB_VERSION, "update_available": False,
           "url": "", "notes": "", "checked_at": now, "error": None, "cached": False}
    try:
        doc = _get_json("%s/repos/%s/releases/latest" % (API, REPO))
        latest = str(doc.get("tag_name") or "").lstrip("vV") or HUB_VERSION
        out.update({
            "latest": latest,
            "url": doc.get("html_url") or "",
            "notes": (doc.get("body") or "")[:4000],
            "zip": doc.get("zipball_url") or "",
            "published_at": doc.get("published_at") or "",
        })
        out["update_available"] = version_tuple(latest) > version_tuple(HUB_VERSION)
    except urllib.error.HTTPError as exc:
        out["error"] = "GitHub HTTP %s" % exc.code
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    _write_cache(out)
    return out


def _safe_target(root, rel):
    target = os.path.normpath(os.path.join(root, rel))
    root_norm = os.path.normpath(root)
    if not target.startswith(root_norm + os.sep):
        return None
    rel_norm = os.path.relpath(target, root_norm).replace(os.sep, "/")
    for pre in PROTECTED:
        if rel_norm == pre.rstrip("/") or rel_norm.startswith(pre):
            return None
    return target


def download(url, timeout=120.0):
    if not url:
        raise ValueError("release 还没有 zip 产物")
    fd, path = tempfile.mkstemp(prefix="hub-update-", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as fh:
            req = urllib.request.Request(url, headers={"User-Agent": "hub-updater/" + HUB_VERSION})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                shutil.copyfileobj(resp, fh)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def apply_zip(zip_path, root=None):
    """把 zip 覆盖到 root（跳过 PROTECTED）。返回 {written, skipped, version}。"""
    root = root or ROOT
    written, skipped, version = 0, 0, ""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = ""
        if names:
            head = names[0].split("/")[0] + "/"
            if all(n.startswith(head) for n in names):
                prefix = head
        for name in names:
            rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
            if not rel or rel.startswith(".github/"):
                continue
            target = _safe_target(root, rel)
            if target is None:
                skipped += 1
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written += 1
                if rel == "hub/__init__.py":
                    try:
                        text = open(target, "r", encoding="utf-8", errors="replace").read()
                        for line in text.splitlines():
                            if line.startswith("HUB_VERSION"):
                                version = line.split("=", 1)[1].strip().strip("\"'")
                                break
                    except OSError:
                        pass
            except OSError:
                skipped += 1
    return {"written": written, "skipped": skipped, "version": version}


def update_to_latest(root=None, force_check=False):
    """检查 → 下载 → 覆盖。返回结构化结果（不重启，由调用方决定）。"""
    info = check(force=force_check)
    if info.get("error"):
        return {"ok": False, "reason": info["error"], "info": info}
    if not info.get("update_available"):
        return {"ok": True, "updated": False, "reason": "already_latest", "info": info}
    try:
        zip_path = download(info.get("zip") or "")
    except Exception as exc:
        return {"ok": False, "reason": "download failed: %s" % exc, "info": info}
    try:
        result = apply_zip(zip_path, root)
    except Exception as exc:
        return {"ok": False, "reason": "apply failed: %s" % exc, "info": info}
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
    _write_cache({"checked_at": 0, "current": HUB_VERSION})
    return {"ok": True, "updated": True, "from": HUB_VERSION,
            "to": result.get("version") or info.get("latest"),
            "written": result["written"], "skipped": result["skipped"],
            "restart_required": True, "info": info}
