# -*- coding: utf-8 -*-
"""智能体一键配置（Qoder 网关版，零第三方依赖）。

把本机 AI 客户端的配置指向本网关：改前自动备份、byte-exact 还原、
多文件客户端两阶段写入（任一失败整体回滚）。设计照 wb_agents.py 的
四条铁律：

1. **非侵入**：纯标准库实现的文本级 YAML / TOML / JSON 编辑器，只增量
   插入或更新 `qoder-proxy` 那一段，绝不重排或抹掉用户的注释、缩进
   与其它 provider 配置。
2. **可回滚**：写入前把原件字节备份到 accounts/agent-backups/（每文件
   保留最新 10 份），首次接入的原件永久保留；restore() byte-exact 回到
   接入前，网关新建的文件会被清理。
3. **密钥安全**：API Key 只写进客户端自己的本地配置目录，网关侧只在
   state 里存 sha256 指纹（不存明文）；单文件超过 8MB 拒绝编辑。
4. **plan 即 apply**：plan() 与 integrate() 共享同一构建器，dry-run 打印
   的就是真实写入的（面板预览与 CLI --dry-run 都走它）。

面板用法（/agents 系列端点，需面板会话）与独立 CLI 用法（客户端装在
别的机器时）见模块尾部 main()。

    python qoder_agents.py list
    python qoder_agents.py plan --client dsh --base-url http://192.168.1.5:8790 --key sk-...
    python qoder_agents.py apply --client dsh --base-url ... --key ...
    python qoder_agents.py restore --client dsh
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import tempfile
import urllib.error
import urllib.request

PROVIDER_ID = "qoder-proxy"
PROVIDER_NAME = "Qoder Gateway"
CREDENTIAL_REF = "QODER_PROXY_API_KEY"
BACKUP_KEEP = 10
MAX_EDIT_BYTES = 8 * 1024 * 1024   # 8MB：单文件超过即拒绝编辑


class AgentConfigError(Exception):
    """用户可读的配置失败（面板与 CLI 都直接展示 str(exc)）。"""


# ---------------------------------------------------------------------------
# 文件原语：原子写 + 读
# ---------------------------------------------------------------------------

def _resolve_home(home=None):
    return str(home or os.path.expanduser("~"))


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    if not os.path.exists(path):
        raise AgentConfigError("file not found: %s" % path)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise AgentConfigError("cannot read %s: %s" % (path, exc))
    if len(data) > MAX_EDIT_BYTES:
        raise AgentConfigError(
            "refusing to edit %s (%d bytes > %d limit)"
            % (path, len(data), MAX_EDIT_BYTES))
    return data


def _read_text(path):
    """读文本配置；缺失=空文档（从零建配置是合法场景，如 DSH 首次生成 settings.yaml）。"""
    if not os.path.exists(path):
        return ""
    data = _read_bytes(path)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise AgentConfigError("%s is not UTF-8 text" % path)


def _atomic_write_text(path, text):
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".qagent.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        raise AgentConfigError("cannot write %s: %s" % (path, exc))


def _prune_empty_parents(path, home=None):
    """删除我们建空的父目录链（restore 后不留「幽灵安装」痕迹）。"""
    home = _resolve_home(home)
    directory = os.path.dirname(os.path.abspath(path))
    stop = os.path.abspath(home)
    while directory and directory != stop and len(directory) > len(stop):
        try:
            if os.listdir(directory):
                break
            os.rmdir(directory)
        except OSError:
            break
        directory = os.path.dirname(directory)


def _load_json(path, what):
    text = _read_text(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AgentConfigError("%s is not valid JSON (%s): %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise AgentConfigError("%s must be a JSON object: %s" % (what, path))
    return data


def _dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def deep_merge(base, patch):
    """递归合并：patch 之外的兄弟键存活；输入不被改动。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 文本级 YAML 编辑器（子集；纯标准库）
# ---------------------------------------------------------------------------

def _yaml_indent(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_key(line):
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s|$)", line.lstrip(" "))
    if not m:
        return None
    return m.group(1)


def _yaml_block_end(lines, start, indent):
    idx = start + 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and _yaml_indent(line) <= indent:
            break
        idx += 1
    return idx


def _yaml_insert_point(lines, start, end, indent):
    idx = end
    while idx > start and not lines[idx - 1].strip():
        idx -= 1
    return idx if idx > start else end


def _yaml_inline_to_block(lines, idx, key, inline_value):
    indent = _yaml_indent(lines[idx])
    value = inline_value.strip()
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        out = [" " * indent + key + ":"]
        if inner:
            for part in inner.split(","):
                part = part.strip()
                if part:
                    out.append(" " * (indent + 2) + part)
        lines[idx:idx + 1] = out
        return
    if value == "":
        return
    raise AgentConfigError(
        "cannot nest keys under `%s: %s` - only an inline {..} mapping "
        "can be expanded" % (key, inline_value))


def yaml_upsert_inline(text, key_path, value):
    """Upsert 嵌套映射键（内联 JSON 值）。块外字节保持不变。"""
    keys = [part.strip() for part in str(key_path).split(".") if part.strip()]
    if not keys:
        raise AgentConfigError("yaml_upsert_inline: empty key path")
    lines = text.split("\n")
    lines = _yaml_upsert_at(lines, 0, len(lines), 0, keys,
                            json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)


def _yaml_upsert_at(lines, start, end, indent, keys, value_json):
    key = keys[0]
    idx = start
    while idx < end:
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == indent and _yaml_key(line) == key:
            after = line.split(":", 1)[1].strip()
            child_end = _yaml_block_end(lines, idx, indent)
            if len(keys) == 1:
                lines[idx] = " " * indent + key + ": " + value_json
                return lines
            if after:
                _yaml_inline_to_block(lines, idx, key, after)
                child_end = _yaml_block_end(lines, idx, indent)
                lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                        indent + 2, keys[1:], value_json)
                return lines
            lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                    indent + 2, keys[1:], value_json)
            return lines
        idx += 1
    insert = _yaml_insert_point(lines, start, end, indent)
    if len(keys) == 1:
        new_lines = [" " * indent + key + ": " + value_json]
    else:
        new_lines = [" " * indent + keys[0] + ":"]
        new_lines.extend(
            _yaml_upsert_at([], 0, 0, indent + 2, keys[1:], value_json))
    lines[insert:insert] = new_lines
    return lines


def _yaml_scalar(value):
    """把值渲染成**始终带双引号**的 YAML 标量。

    YAML 1.2 core schema 会把 `13638719007`、`007`、`1e5`、`0x1f`、`-1`、
    `true`、`false`、`null`、`~` 这类裸值解析成非字符串类型。DSH 的
    credentials-local 对 `refs` 的值做硬校验（typeof !== "string" 直接抛
    TypeError），写入一个纯数字 Key 就会让 `credentials` 插件不激活，
    进而 connection / authorization / 全部 tauri 与 mcp 插件级联 waiting，
    dsh 完全无法启动（issue #103）。

    JSON 的双引号标量同时是合法的 YAML double-quoted scalar，转义规则也
    覆盖换行、引号、反斜杠与控制字符，比手写 `'%s'` 安全。
    """
    return json.dumps(str(value), ensure_ascii=False)


def refs_value_is_scalar_string(text, key):
    """refs: 块里该键的值是不是一个带引号的标量（issue #103 写后自检）。

    只做本文件写入格式的自检：我们一律用 JSON 双引号标量写值，所以只要该行
    的值不是以 `"` 开头并以 `"` 结尾，就说明序列化路径被改坏了（或不慎写了
    裸值），DSH 的 credentials-local 会直接抛 TypeError。
    """
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith(key + ":"):
            continue
        value = stripped.partition(":")[2].strip()
        if len(value) >= 2 and value.startswith(chr(34)) and value.endswith(chr(34)):
            return True
        return False
    return False


def yaml_refs_upsert(text, key, value):
    """Upsert 顶层 `refs:` 块内的键（DSH credentials.yaml 专用）。"""
    rendered = _yaml_scalar(value)
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == 0 and _yaml_key(line) == "refs":
            after = line.split(":", 1)[1].strip()
            if after:
                raise AgentConfigError(
                    "cannot upsert into `refs: %s` - only a block-style "
                    "refs: mapping is supported" % after)
            end = _yaml_block_end(lines, idx, 0)
            j = idx + 1
            while j < end:
                if lines[j].strip() and _yaml_indent(lines[j]) == 2 \
                        and _yaml_key(lines[j]) == key:
                    lines[j] = "  %s: %s" % (key, rendered)
                    return "\n".join(lines)
                j += 1
            insert = _yaml_insert_point(lines, idx + 1, end, 2)
            lines[insert:insert] = ["  %s: %s" % (key, rendered)]
            return "\n".join(lines)
        idx += 1
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("refs:")
    lines.append("  %s: %s" % (key, rendered))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 文本级 TOML 编辑器（子集；纯标准库）
# ---------------------------------------------------------------------------

_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def toml_upsert_top_key(text, key, value):
    """Upsert 顶层 `key = value`（写到第一个 [table] 前）。"""
    lines = text.split("\n")
    rendered = "%s = %s" % (key, _toml_value(value))
    idx = 0
    first_table = len(lines)
    while idx < len(lines):
        if _TOML_TABLE_RE.match(lines[idx]):
            first_table = idx
            break
        m = _TOML_KEY_RE.match(lines[idx])
        if m and m.group(1) == key:
            lines[idx] = rendered
            return "\n".join(lines)
        idx += 1
    # 找顶层段最后一个非空行插入
    insert = first_table
    while insert > 0 and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, rendered)
    return "\n".join(lines)


def toml_upsert_table(text, table, mapping):
    """Upsert `[table]` 下的键值（表不存在则建在文件末尾）。"""
    lines = text.split("\n")
    rendered = ["[%s]" % table]
    for k, v in mapping.items():
        rendered.append("%s = %s" % (k, _toml_value(v)))
    idx = 0
    while idx < len(lines):
        m = _TOML_TABLE_RE.match(lines[idx])
        if m and m.group(1) == table:
            end = idx + 1
            while end < len(lines) and not _TOML_TABLE_RE.match(lines[end]):
                end += 1
            body = dict(mapping)
            j = idx + 1
            while j < end:
                km = _TOML_KEY_RE.match(lines[j])
                if km and km.group(1) in body:
                    lines[j] = "%s = %s" % (km.group(1), _toml_value(body.pop(km.group(1))))
                j += 1
            if body:
                insert = end
                while insert > idx + 1 and not lines[insert - 1].strip():
                    insert -= 1
                extra = ["%s = %s" % (k, _toml_value(v)) for k, v in body.items()]
                lines[insert:insert] = extra
            return "\n".join(lines)
        idx += 1
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("")
    lines.extend(rendered)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 状态与备份（存网关侧 accounts/；CLI 模式存 ~/.qoder-agent-config/）
# ---------------------------------------------------------------------------

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, "agent-state.json")


def _load_state(accounts_dir):
    path = _state_path(accounts_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(accounts_dir, state):
    path = _state_path(accounts_dir)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def _backup_dir(accounts_dir, client_id):
    return os.path.join(accounts_dir, "agent-backups", client_id)


def _backup_file(accounts_dir, client_id, path, original):
    directory = _backup_dir(accounts_dir, client_id)
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = os.path.basename(path) + "." + stamp + ".bak"
    target = os.path.join(directory, name)
    with open(target, "wb") as fh:
        fh.write(original)
    _prune_backups(directory)
    return target


def _prune_backups(directory, keep=BACKUP_KEEP):
    """每文件只留最新 keep 份；首次接入的原件（.first.bak）永久保留。"""
    if not os.path.isdir(directory):
        return
    groups = {}
    for name in os.listdir(directory):
        if not name.endswith(".bak"):
            continue
        base = name.rsplit(".", 3)[0] if name.count(".") >= 3 else name
        groups.setdefault(base, []).append(name)
    for base, names in groups.items():
        if len(names) <= keep:
            continue
        ordered = sorted(names)   # 时间戳前缀 → 字典序即时间序
        for name in ordered[:len(names) - keep]:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 各客户端 apply：输入 ctx，输出 {path: new_text}
# ---------------------------------------------------------------------------

def _claude_settings_path(home):
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(override, "settings.json")
    return os.path.join(home, ".claude", "settings.json")


def _apply_claude_code(paths, ctx):
    path = paths["settings"]
    data = _load_json(path, "claude settings")
    env = dict(data.get("env") or {})
    # Claude Code 自己拼 /v1：给它裸根地址（原生 Anthropic Messages 端点）。
    env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1/?$", "", ctx["base_url"])
    env["ANTHROPIC_AUTH_TOKEN"] = ctx["api_key"]
    if ctx.get("model"):
        for name in ("ANTHROPIC_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[name] = ctx["model"]
    data["env"] = env
    return {path: _dump_json(data)}


def _codex_home(home):
    return os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")


def _apply_codex(paths, ctx):
    config_path = paths["config"]
    text = _read_text(config_path)
    text = toml_upsert_top_key(text, "model_provider", PROVIDER_ID)
    if ctx.get("model"):
        text = toml_upsert_top_key(text, "model", ctx["model"])
    text = toml_upsert_table(text, "model_providers." + PROVIDER_ID, {
        "name": PROVIDER_NAME,
        "base_url": ctx["base_url_v1"],
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
    })
    auth_path = paths["auth"]
    auth = _load_json(auth_path, "codex auth")
    auth = deep_merge(auth, {"OPENAI_API_KEY": ctx["api_key"]})
    return {config_path: text, auth_path: _dump_json(auth)}


def _apply_opencode(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "opencode config")
    models = {}
    for entry in ctx.get("models") or ([{"id":ctx.get("model") or "auto"}]):
        mid = entry.get("id")
        if not mid:
            continue
        item = {"name": mid}
        limits = {}
        if entry.get("context_window"):
            limits["context"] = entry["context_window"]
        if entry.get("max_output"):
            limits["output"] = entry["max_output"]
        if limits:
            item["limit"] = limits
        models[mid] = item
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": PROVIDER_NAME,
        "options": {
            "baseURL": ctx["base_url_v1"],
            "apiKey": ctx["api_key"],
        },
        "models": models,
    }
    data = deep_merge(data, {"provider": {PROVIDER_ID: provider}})
    if ctx.get("model"):
        data["model"] = PROVIDER_ID + "/" + ctx["model"]
    return {path: _dump_json(data)}


def _dsh_home(home):
    return os.environ.get("DSH_HOME") or os.path.join(home, ".dsh")


def _apply_dsh(paths, ctx):
    settings_path = paths["settings"]
    text = _read_text(settings_path)
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid}
        if entry.get("context_window"):
            item["contextWindow"] = entry["context_window"]
        models.append(item)
    provider = {
        "displayName": PROVIDER_NAME,
        "apiKeyEnv": CREDENTIAL_REF,
        "api": "openai-completions",
        "baseURL": ctx["base_url_v1"],
    }
    if models:
        provider["models"] = models
    text = yaml_upsert_inline(
        text, "llm-pi-ai.providers.%s" % PROVIDER_ID, provider)
    if ctx.get("model"):
        text = yaml_upsert_inline(
            text, "agent-default-model",
            {"provider": PROVIDER_ID, "model": ctx["model"]})
    credentials_path = paths["credentials"]
    cred_text = _read_text(credentials_path)
    cred_text = yaml_refs_upsert(cred_text, CREDENTIAL_REF, ctx["api_key"])
    # issue #103：写后自检——DSH 要求 refs 的值必须是字符串标量；
    # 这里失败就当场报错，绝不把「配置成功」写进面板。
    if not refs_value_is_scalar_string(cred_text, CREDENTIAL_REF):
        raise ValueError(
            "DSH credentials 自检失败：refs.%s 未写成带引号的字符串标量"
            "（纯数字/布尔 Key 会被 YAML 解析成非字符串，DSH 将无法启动）"
            % CREDENTIAL_REF)
    return {settings_path: text, credentials_path: cred_text}


def _apply_crush(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "crush config")
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid, "name": mid}
        if entry.get("context_window"):
            item["context_window"] = entry["context_window"]
        if entry.get("max_output"):
            item["default_max_tokens"] = entry["max_output"]
        models.append(item)
    provider = {
        "name": PROVIDER_NAME,
        "type": "openai",
        "base_url": ctx["base_url_v1"],
        "api_key": ctx["api_key"],
        "models": models,
    }
    data = deep_merge(data, {"providers": {PROVIDER_ID: provider}})
    return {path: _dump_json(data)}


# ---------------------------------------------------------------------------
# 客户端注册表
# ---------------------------------------------------------------------------

CLIENTS = {
    "claude-code": {
        "id": "claude-code",
        "label": "Claude Code",
        "desc": "Anthropic CLI；走网关原生 /v1/messages（ANTHROPIC_BASE_URL）。",
        "protocol": "anthropic",
        "files": lambda home: {
            "settings": _claude_settings_path(home),
        },
        "probe": lambda home: (
            os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR")
                         or os.path.join(home, ".claude"))
            or os.path.isfile(_claude_settings_path(home))
            or bool(shutil.which("claude"))
        ),
        "apply": _apply_claude_code,
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "desc": "OpenAI Codex CLI；注册 qoder-proxy Responses provider。",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(_codex_home(home), "config.toml"),
            "auth": os.path.join(_codex_home(home), "auth.json"),
        },
        "probe": lambda home: (
            os.path.isdir(_codex_home(home))
            or os.path.isfile(os.path.join(_codex_home(home), "config.toml"))
            or bool(shutil.which("codex"))
        ),
        "apply": _apply_codex,
    },
    "opencode": {
        "id": "opencode",
        "label": "OpenCode",
        "desc": "OpenCode；注册 openai-compatible provider。",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "opencode", "opencode.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "opencode"))
            or os.path.isfile(os.path.join(home, ".config", "opencode",
                                           "opencode.json"))
            or bool(shutil.which("opencode"))
        ),
        "apply": _apply_opencode,
    },
    "dsh": {
        "id": "dsh",
        "label": "DSH (DeepSeek Harness)",
        "desc": "DSH；llm-pi-ai 注册 qoder-proxy provider，Key 走 credentials refs。",
        "protocol": "openai",
        "files": lambda home: {
            "settings": os.path.join(_dsh_home(home), "settings.yaml"),
            "credentials": os.path.join(_dsh_home(home), ".credentials.yaml"),
        },
        "probe": lambda home: (
            os.path.isdir(_dsh_home(home))
            or os.path.isfile(os.path.join(_dsh_home(home), "settings.yaml"))
            or bool(shutil.which("dsh"))
        ),
        "apply": _apply_dsh,
    },
    "crush": {
        "id": "crush",
        "label": "Crush (Charm)",
        "desc": "Charm Crush；providers 注册 openai 型 qoder-proxy。",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "crush", "crush.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "crush"))
            or os.path.isfile(os.path.join(home, ".config", "crush",
                                           "crush.json"))
            or bool(shutil.which("crush"))
        ),
        "apply": _apply_crush,
    },
}


def extend_clients(core):
    clients = core.CLIENTS
    config_root = lambda home: os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    def json_adapter(build):
        def apply(paths, ctx):
            path = paths["config"]
            return {path: core._dump_json(build(core._load_json(path, "client config"), ctx))}
        return apply
    def model_rows(ctx):
        rows = [dict(m) for m in ctx.get("models") or [] if m.get("id")]
        selected = ctx.get("model") or (rows[0]["id"] if rows else "auto")
        if not any(m["id"] == selected for m in rows):
            rows.insert(0, {"id": selected})
        return rows, selected
    def path(home, *parts):
        return os.path.join(home, *parts)
    def register(cid, label, protocol, parts, apply, docs, desc, command=None, dirs=None):
        files = parts if callable(parts) else lambda home: {"config": path(home, *parts)}
        clients[cid] = {"id": cid, "label": label, "protocol": protocol,
            "desc": desc, "files": files, "apply": apply, "docs_url": docs,
            "category": "CLI / Harness" if cid != "continue" else "IDE 扩展",
            "mode": "automatic", "commands": [command or cid], "probe_dirs": dirs or [],
            "probe": lambda home: any(os.path.exists(p) for p in files(home).values())
                or bool(shutil.which(command or cid)) or any(os.path.isdir(path(home, d)) for d in (dirs or []))}
    def aider(paths, ctx):
        p = paths["config"]
        text = core._read_text(p)
        for name, value in (("openai-api-base",ctx["base_url_v1"]), ("openai-api-key",ctx["api_key"]),
                            ("model","openai/"+(ctx.get("model") or "auto"))):
            text = core.yaml_upsert_inline(text, name, value)
        return {p:text}
    register("aider", "Aider", "openai", (".aider.conf.yml",), aider,
        "https://aider.chat/docs/config/options.html", "更新用户 .aider.conf.yml；保留原注释与其他设置。")
    def continue_apply(paths, ctx):
        p=paths["config"];text=core._read_text(p)
        # Continue accepts inline JSON as YAML sequence values; preserve other model blocks.
        rows, selected=model_rows(ctx)
        model={"name":"Qoder Gateway", "provider":"openai", "model":selected,
               "apiBase":ctx["base_url_v1"], "apiKey":ctx["api_key"],
               "roles":["chat","edit","apply"], "useResponsesApi":False}
        text=managed_block(text, "continue-model", "  - "+json.dumps(model,ensure_ascii=False))
        # Managed block must be under models, not arbitrarily below the last top-level key.
        begin="# qoder-proxy:continue-model:begin";end="# qoder-proxy:continue-model:end"
        block=re.search(r"(?m)^"+re.escape(begin)+r"\n[\s\S]*?^"+re.escape(end)+r"\n?",text)
        if not block:raise core.AgentConfigError("Continue managed block missing")
        model_block=block.group(0);text=text[:block.start()]+text[block.end():]
        lines=text.splitlines(keepends=True)
        indices=[i for i,l in enumerate(lines) if re.match(r"^models\s*:",l)]
        if len(indices)>1:raise core.AgentConfigError("duplicate Continue models keys")
        if indices:
            i=indices[0]
            if not re.match(r"^models\s*:\s*(?:#.*)?$",lines[i].rstrip()):
                raise core.AgentConfigError("Continue models is not a block list; use manual merge")
            j=i+1
            while j<len(lines) and (not lines[j].strip() or lines[j].startswith((" ","#"))):j+=1
            lines.insert(j,model_block if model_block.endswith("\n") else model_block+"\n")
            text="".join(lines)
        else:
            if not text.strip():text='name: Qoder Local\nversion: 1.0.0\nschema: v1\n'
            text=text.rstrip()+"\nmodels:\n"+model_block
        return {p:text}
    register("continue", "Continue", "openai", (".continue","config.yaml"), continue_apply,
        "https://docs.continue.dev/reference", "向 Continue 用户 YAML 增量添加网关模型；不改项目配置。", command="cn", dirs=[".continue"])
    def pi(data,ctx):
        rows,_=model_rows(ctx)
        provider={"baseUrl":ctx["base_url_v1"],"api":"openai-completions","apiKey":ctx["api_key"],
                  "models":[dict({"id":m["id"]},**({"contextWindow":m["context_window"]} if m.get("context_window") else {})) for m in rows]}
        return core.deep_merge(data,{"providers":{core.PROVIDER_ID:provider}})
    register("pi", "Pi Coding Agent", "openai", lambda h:{"config":path(os.environ.get("PI_CODING_AGENT_DIR") or path(h,".pi","agent"),"models.json")},json_adapter(pi),
        "https://pi.dev/docs/latest/models", "注册 models.json 自定义 provider；不触碰 auth.json。",dirs=[".pi"])
    def factory(data,ctx):
        rows,_=model_rows(ctx)
        previous=data.get("customModels",[])
        if not isinstance(previous,list):raise core.AgentConfigError("customModels must be an array")
        keep=[m for m in previous if not (isinstance(m,dict) and str(m.get("displayName","")).startswith("Qoder Gateway · "))]
        data=dict(data);data["customModels"]=keep+[{"model":m["id"],"displayName":"Qoder Gateway · "+m["id"],
            "baseUrl":ctx["base_url_v1"],"apiKey":ctx["api_key"],"provider":"generic-chat-completion-api",
            "maxOutputTokens":int(m.get("max_output") or 8192)} for m in rows]
        return data
    register("factory-droid", "Factory Droid", "openai", (".factory","settings.json"),json_adapter(factory),
        "https://docs.factory.com/model-independence/byok", "保留已有 customModels，添加网关 BYOK 模型。",command="droid",dirs=[".factory"])
    def copilot(data,ctx):
        rows,_=model_rows(ctx);data=dict(data)
        providers=data.get("providers",[]);models=data.get("models",[])
        if not isinstance(providers,list) or not isinstance(models,list):raise core.AgentConfigError("Copilot providers/models must be arrays")
        data["providers"]=[p for p in providers if not isinstance(p,dict) or p.get("name")!=core.PROVIDER_ID]+[{"name":core.PROVIDER_ID,"type":"openai","baseUrl":ctx["base_url_v1"],"apiKey":ctx["api_key"]}]
        data["models"]=[m for m in models if not isinstance(m,dict) or m.get("provider")!=core.PROVIDER_ID]+[{"id":m["id"],"provider":core.PROVIDER_ID} for m in rows]
        return data
    register("copilot-cli", "GitHub Copilot CLI", "openai",lambda h:{"config":os.environ.get("COPILOT_PROVIDERS_CONFIG") or path(h,".copilot","providers.json")},json_adapter(copilot),
        "https://docs.github.com/copilot/how-tos/copilot-cli/customize-copilot/add-llm-models", "写入官方 providers.json BYOK 注册表，保留 GitHub 默认账号。",command="copilot",dirs=[".copilot"])
    def openhands(data,ctx):
        return core.deep_merge(data,{"llm":{"model":"openai/"+(ctx.get("model") or "auto"),"api_key":ctx["api_key"],"base_url":ctx["base_url_v1"]}})
    register("openhands", "OpenHands CLI", "openai", (".openhands","agent_settings.json"),json_adapter(openhands),
        "https://docs.openhands.dev/openhands/usage/cli/command-reference", "更新官方 agent_settings.json 的 llm 字段。",dirs=[".openhands"])
    def kilo(paths,ctx):
        data=core._load_json(paths["config"],"Kilo config");rows,selected=model_rows(ctx)
        data=core.deep_merge(data,{"provider":{core.PROVIDER_ID:{"npm":"@ai-sdk/openai-compatible","name":core.PROVIDER_NAME,
            "options":{"baseURL":ctx["base_url_v1"],"apiKey":ctx["api_key"]},
            "models":{m["id"]:{"name":m["id"]} for m in rows}}},"model":core.PROVIDER_ID+"/"+selected})
        return {paths["config"]:core._dump_json(data)}
    def kilo_files(h):
        d=path(config_root(h),"kilo")
        existing=path(d,"kilo.jsonc")
        # Use actual existing file; JSONC with comments refuses rather than destroying comments.
        return {"config":existing if os.path.exists(existing) else path(d,"kilo.json")}
    register("kilo-cli", "Kilo Code CLI", "openai",kilo_files,kilo,
        "https://kilo.ai/docs/code-with-ai/platforms/cli", "注册自定义 OpenAI provider；复杂 JSONC 保守拒绝并给出手动方案。",command="kilo",dirs=[".config/kilo"])
    def vibe(paths,ctx):
        p=paths["config"];text=core._read_text(p)
        # Only our tagged TOML array blocks; all other tables untouched.
        block='[[providers]]\nname = "qoder-proxy"\napi_base = '+json.dumps(ctx["base_url_v1"])+ '\napi_key_env_var = "QODER_PROXY_API_KEY"\napi_style = "openai"\nbackend = "generic"\n\n[[models]]\nname = '+json.dumps(ctx.get("model") or "auto")+'\nprovider = "qoder-proxy"\nalias = "qoder-proxy"'
        text=managed_block(text,"vibe-provider",block)
        # Insert global active_model before any TOML table, including [[array]].
        lines=text.splitlines();idx=next((i for i,l in enumerate(lines) if l.lstrip().startswith("[")),len(lines))
        matches=[i for i in range(idx) if re.match(r"^\s*active_model\s*=",lines[i])]
        if matches:lines[matches[0]]='active_model = "qoder-proxy"'
        else:lines.insert(idx,'active_model = "qoder-proxy"')
        env=core._read_text(paths["env"]);env=managed_block(env,"vibe-key","QODER_PROXY_API_KEY="+json.dumps(ctx["api_key"]))
        return {p:"\n".join(lines)+"\n",paths["env"]:env}
    register("mistral-vibe", "Mistral Vibe", "openai",lambda h:{"config":path(h,".vibe","config.toml"),"env":path(h,".vibe",".env")},vibe,
        "https://docs.mistral.ai/vibe/code/cli/configuration", "添加命名 provider/model 和独立凭据变量；保留原 Mistral 设置。",command="vibe",dirs=[".vibe"])
    manuals=[
      ("goose","Goose","CLI / Harness","goose",[".config/goose"],[],"https://goose-docs.ai/docs/getting-started/providers/","goose configure → Custom Providers / OpenAI Compatible；Base URL={BASE_URL_V1}；Key=YOUR_API_KEY；模型={MODEL}。凭据由客户端 keychain 管理。"),
      ("gemini-cli","Gemini CLI","CLI / Harness","gemini",[".gemini"],[],"https://geminicli.com/docs/","原生使用 Gemini 协议，不能直接接本项目 OpenAI/Anthropic 出口；请保留其原生配置。"),
      ("qwen-code","Qwen Code","CLI / Harness","qwen",[".qwen"],[],"https://qwenlm.github.io/qwen-code-docs/en/users/configuration/","使用 OpenAI-compatible provider；OPENAI_BASE_URL={BASE_URL_V1}；OPENAI_API_KEY=YOUR_API_KEY；OPENAI_MODEL={MODEL}。"),
      ("amp","Amp","CLI / Harness","amp",[".config/amp"],[],"https://ampcode.com/manual","使用其官方模型路由；当前不自动改写内部凭据或将普通 API Key 冒充 Amp 登录。"),
      ("cline","Cline","IDE 扩展","cline",[],["saoudrizwan.claude-dev"],"https://docs.cline.bot/provider-config/openai-compatible","Cline 设置 → API Provider=OpenAI Compatible；Base URL={BASE_URL_V1}；API Key=YOUR_API_KEY；Model ID={MODEL}。"),
      ("roo-code","Roo Code","IDE 扩展","",[],["rooveterinaryinc.roo-cline"],"https://docs.roocode.com/providers/openai-compatible","Roo Code 设置 → OpenAI Compatible；Base URL={BASE_URL_V1}；Key=YOUR_API_KEY；Model={MODEL}。"),
      ("kilo-vscode","Kilo Code (VS Code)","IDE 扩展","",[],["kilocode.kilo-code"],"https://kilo.ai/docs/ai-providers/openai-compatible","Providers → Custom provider → OpenAI Compatible；Base URL={BASE_URL_V1}，Key=YOUR_API_KEY，模型={MODEL}。"),
      ("cursor","Cursor","IDE / 桌面","cursor",[".cursor"],[],"https://docs.cursor.com/settings/api-keys","Settings → Models → OpenAI API Key / Override OpenAI Base URL；URL={BASE_URL_V1}，Key=YOUR_API_KEY，添加模型={MODEL}。部分Agent功能由客户端限制。"),
      ("windsurf","Windsurf","IDE / 桌面","windsurf",[".codeium/windsurf",".windsurf"],[],"https://docs.windsurf.com/","使用客户端 BYOK 支持范围；不直接编辑私有状态数据库，网关不能替代 Windsurf 原生订阅能力。"),
      ("trae","Trae","IDE / 桌面","trae",[".trae"],[],"https://docs.trae.ai/","设置 → 模型 → 自定义 OpenAI-compatible provider；URL={BASE_URL_V1}，Key=YOUR_API_KEY，模型={MODEL}。"),
      ("zed","Zed","IDE / 桌面","zed",[".config/zed"],[],"https://zed.dev/docs/ai/llm-providers","Settings → AI → OpenAI Compatible；api_url={BASE_URL_V1}，模型={MODEL}；凭据在 Zed 自己的安全存储录入。"),
      ("cline-cli","Cline CLI","CLI / Harness","cline",[".cline"],[],"https://docs.cline.bot/cline-cli/overview","运行 cline auth，选择 OpenAI Compatible，填 {BASE_URL_V1}、YOUR_API_KEY、{MODEL}。"),
      ("junie","JetBrains Junie","IDE / 桌面","junie",[".junie"],[],"https://www.jetbrains.com/help/junie/","使用客户端官方 BYOK 设置；插件/订阅功能不由本网关自动接管。"),
      ("augment","Augment Code","IDE 扩展","auggie",[".augment"],["augment.vscode-augment"],"https://docs.augmentcode.com/","使用 Augment 官方提供的模型/接入方式；不改写其私有登录数据库。"),
      ("cherry-studio","Cherry Studio","IDE / 桌面","",[],[],"https://docs.cherry-ai.com/","设置 → 模型服务 → OpenAI；地址={BASE_URL_V1}，Key=YOUR_API_KEY，获取模型。"),
      ("chatbox","Chatbox","IDE / 桌面","chatbox",[],[],"https://chatboxai.app/","Settings → Model Provider → Custom/OpenAI API；URL={BASE_URL_V1}，Key=YOUR_API_KEY，模型={MODEL}。"),
      ("lobe-chat","LobeChat","Web / 自部署","",[],[],"https://lobehub.com/docs","Provider → OpenAI Compatible；Base URL={BASE_URL_V1}，Key=YOUR_API_KEY。部署在其他机器时不要使用网关127.0.0.1。"),
      ("open-webui","Open WebUI","Web / 自部署","open-webui",[],[],"https://docs.openwebui.com/getting-started/quick-start/starting-with-openai-compatible/","Admin → Connections → OpenAI API，填 {BASE_URL_V1} 和 YOUR_API_KEY；容器须使用可达宿主地址。"),
    ]
    for cid,label,category,command,dirs,extensions,docs,guide in manuals:
        clients[cid]={"id":cid,"label":label,"desc":"提供本机检测与明确接入指引，不直接改私有状态或凭据库。",
            "protocol":"native" if cid in ("gemini-cli","amp","windsurf","junie","augment") else "openai",
            "files":lambda h:{},"probe":lambda h:False,"apply":None,"mode":"manual",
            "category":category,"commands":[command] if command else [],"probe_dirs":dirs,
            "extensions":extensions,"docs_url":docs,"guide":guide}


def managed_block(text, name, body):
    begin="# qoder-proxy:"+name+":begin";end="# qoder-proxy:"+name+":end"
    if text.count(begin)!=text.count(end) or text.count(begin)>1:
        raise ValueError("invalid existing Qoder managed block")
    block=begin+"\n"+body.rstrip()+"\n"+end+"\n"
    if begin in text:
        return re.sub(r"(?m)^"+re.escape(begin)+r"\n[\s\S]*?^"+re.escape(end)+r"\n?",lambda m:block,text)
    return text.rstrip()+"\n"+block


def detect_catalog(clients, home):
    """One directory inventory per extension root; never reads config contents."""
    extensions=[]
    for relative in (".vscode/extensions",".vscode-insiders/extensions",".cursor/extensions",".windsurf/extensions"):
        directory=os.path.join(home,relative)
        try:
            extensions.extend(name.lower() for name in os.listdir(directory))
        except OSError:
            pass
    apps=[]
    for directory in (os.environ.get("LOCALAPPDATA"),os.environ.get("APPDATA"),"/Applications",os.path.join(home,"Applications")):
        if not directory:continue
        try:apps.extend(name.lower() for name in os.listdir(directory))
        except OSError:pass
    result={}
    for cid,client in clients.items():
        evidence=[]
        for cmd in client.get("commands",[]):
            executable=shutil.which(cmd)
            if executable:evidence.append("PATH: "+os.path.basename(executable))
        for relative in client.get("probe_dirs",[]):
            if os.path.isdir(os.path.join(home,relative)):evidence.append("目录: "+relative)
        for prefix in client.get("extensions",[]):
            if any(e==prefix or e.startswith(prefix+"-") for e in extensions):evidence.append("扩展: "+prefix)
        app_names={"cherry-studio":["cherry studio","cherrystudio"],"chatbox":["chatbox"],"cursor":["cursor"],"windsurf":["windsurf"],"trae":["trae"]}.get(cid,[])
        if any(any(n in app for n in app_names) for app in apps):evidence.append("应用目录")
        result[cid]=evidence
    return result


extend_clients(sys.modules[__name__])
CLIENTS["continue"]["extensions"] = ["continue.continue"]

def template(client_id, base_url, model=None):
    """Pure placeholder template: build in an empty temp dir; no local config/key reads."""
    client = _client(client_id)
    base = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise AgentConfigError("base_url must start with http:// or https://")
    base_v1 = base if base.endswith("/v1") else base+"/v1"
    model = str(model or "auto")
    guide = client.get("guide", "在客户端选择 Qoder Gateway provider / 模型；配置前保留原件。")
    guide = guide.replace("{BASE_URL_V1}", base_v1).replace("{MODEL}", model)
    if client.get("mode") == "manual":
        return {"client": client_id, "mode": "manual", "guide": guide, "files": []}
    with tempfile.TemporaryDirectory(prefix="qoder-agent-template-") as home:
        # Only role names come from the registry. Never read any resolved local client path.
        roles = client["files"](home)
        paths = {role: os.path.join(home, role + ".template") for role in roles}
        ctx = {"base_url": base, "base_url_v1": base_v1, "api_key": "YOUR_API_KEY",
               "model": model, "models": [{"id": model}]}
        contents = client["apply"](paths, ctx)
        return {"client": client_id, "mode": "automatic", "guide": guide,
                "files": [{"role": role, "content": contents[p]} for role,p in paths.items() if p in contents]}


def _client(client_id):
    client = CLIENTS.get(client_id)
    if not client:
        raise AgentConfigError("unknown client %r (have: %s)"
                               % (client_id, ", ".join(sorted(CLIENTS))))
    return client


# ---------------------------------------------------------------------------
# 三主入口：overview / plan / integrate / restore
# ---------------------------------------------------------------------------

def overview(accounts_dir, home=None):
    """本机探测：装了哪些客户端、各自的接入状态（只读，不碰任何文件）。"""
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    out = []
    evidence_map = detect_catalog(CLIENTS, home)
    for client_id in sorted(CLIENTS):
        client = CLIENTS[client_id]
        evidence = evidence_map.get(client_id, [])
        installed = bool(evidence or client["probe"](home))
        if installed and not evidence:
            evidence = ["配置目录 / 可执行文件"]
        record = state.get(client_id) if installed else None
        out.append({
            "id": client_id,
            "label": client["label"],
            "desc": client["desc"],
            "protocol": client["protocol"],
            "installed": installed,
            "mode": client.get("mode", "automatic"),
            "category": client.get("category", "CLI / Harness"),
            "evidence": evidence,
            "docs_url": client.get("docs_url", ""),
            "guide": client.get("guide", "使用下方占位符示例或一键配置；写入前备份，可还原。"),
            "config_paths": list(client["files"](home).values()),
            "configured": bool(isinstance(record, dict)
                               and record.get("files")),
            "applied_at": (record or {}).get("applied_at"),
            "base_url": (record or {}).get("base_url"),
            "model": (record or {}).get("model"),
        })
    return {"clients": out}


def plan(client_id, base_url, api_key, model=None, models=None, home=None):
    """integrate() 会写的确切文件内容；不落盘。dry-run 与真实写入共享它。"""
    client = _client(client_id)
    home = _resolve_home(home)
    base_url = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise AgentConfigError("base_url must start with http:// or https://")
    base_url_v1 = base_url if base_url.endswith("/v1") else base_url + "/v1"
    ctx = {
        "base_url": base_url,
        "base_url_v1": base_url_v1,
        "api_key": str(api_key or ""),
        "model": str(model or "").strip() or None,
        "models": list(models or []),
    }
    if client.get("mode") == "manual":
        raise AgentConfigError("该客户端仅支持检测与手动接入，请查看官方配置指引")
    paths = client["files"](home)
    try:
        new_contents = client["apply"](paths, ctx)
    except AgentConfigError:
        raise
    except Exception as exc:
        raise AgentConfigError("failed to build %s config: %s"
                               % (client_id, exc))
    files = []
    for path in paths.values():
        content = new_contents.get(path)
        if content is None:
            continue
        files.append({
            "path": path,
            "content": content,
            "existed": os.path.exists(path),
        })
    return {
        "client": client_id,
        "label": client["label"],
        "base_url": base_url,
        "model": ctx["model"],
        "files": files,
    }


def integrate(accounts_dir, client_id, base_url, api_key, model=None,
              models=None, home=None):
    """把 client_id 指向本网关；返回写入明细。

    首次 apply 备份每个被碰文件的原件字节；后续 apply 保留最早那份备份
    （restore 永远回到**第一次接入前**）。多文件客户端两阶段写入：
    任一文件失败，已写文件整体回滚。
    """
    built = plan(client_id, base_url, api_key, model=model, models=models,
                 home=home)
    state = _load_state(accounts_dir)
    record = state.get(client_id) or {}
    previous_files = {entry.get("path"): entry
                      for entry in record.get("files") or []
                      if isinstance(entry, dict)}
    # Phase 1：先备份，后动手。
    staged = []
    rollback_bytes = {}
    for entry in built["files"]:
        path, content, existed = entry["path"], entry["content"], entry["existed"]
        rollback_bytes[path] = _read_bytes(path) if os.path.exists(path) else None
        prior = previous_files.get(path)
        if prior is not None and not prior.get("existed") and existed:
            # 上次 apply 我们建的文件：仍是原样就还是「我们的」，restore 删它
            # 而不是复活我们写的字节（可能含 Key）；被手改过才算用户的文件。
            try:
                if _sha256_bytes(_read_bytes(path)) == prior.get("sha256"):
                    existed = False
            except AgentConfigError:
                pass
        backup = None
        if existed and prior and prior.get("backup"):
            backup = prior.get("backup")
        elif existed:
            backup = _backup_file(accounts_dir, client_id, path,
                                  _read_bytes(path))
        staged.append((path, content, existed, backup))

    # Phase 2：事务式写入；失败回滚已写的。
    written = []
    entries = []
    try:
        for path, content, existed, backup in staged:
            _atomic_write_text(path, content)
            written.append((path, existed, backup))
            entries.append({
                "path": path,
                "existed": existed,
                "backup": backup,
                "sha256": _sha256_bytes(content.encode("utf-8")),
                "bytes": len(content.encode("utf-8")),
            })
    except Exception as exc:
        for w_path, w_existed, w_backup in reversed(written):
            try:
                if rollback_bytes[w_path] is not None:
                    _atomic_write_bytes(w_path, rollback_bytes[w_path])
                elif os.path.exists(w_path):
                    os.unlink(w_path)
            except Exception:
                pass
        raise AgentConfigError("failed to write %s files: %s" % (client_id, exc))

    state[client_id] = {
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": built["label"],
        "applied_at": state[client_id]["applied_at"],
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }


def restore(accounts_dir, client_id, home=None):
    """撤销 integrate()：byte-exact 放回原件；我们建的文件删除。"""
    client = _client(client_id)
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    record = state.get(client_id)
    if not isinstance(record, dict) or not record.get("files"):
        raise AgentConfigError(
            "%s has no recorded integration to restore" % client["label"])
    restored = []
    for entry in record.get("files") or []:
        path = entry.get("path")
        if not path:
            continue
        if entry.get("existed") and entry.get("backup"):
            backup = entry["backup"]
            if not os.path.exists(backup):
                raise AgentConfigError(
                    "backup missing for %s: %s" % (path, backup))
            _atomic_write_bytes(path, _read_bytes(backup))
            restored.append({"path": path, "action": "restored"})
        else:
            try:
                os.remove(path)
                restored.append({"path": path, "action": "deleted"})
            except FileNotFoundError:
                restored.append({"path": path, "action": "already-absent"})
            _prune_empty_parents(path, home)
    state.pop(client_id, None)
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": client["label"],
        "restored": restored,
    }


def fetch_models_from_gateway(base_url, api_key, timeout=10):
    """网关模型清单（顺带验证 Key：401 = Key 被拒）。"""
    base = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise AgentConfigError("base_url must start with http:// or https://")
    if base.endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        base + "/v1/models",
        headers={"Authorization": "Bearer " + str(api_key or "")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AgentConfigError(
                "the gateway rejected the API key (HTTP 401)")
        raise AgentConfigError("gateway /v1/models failed: HTTP %s" % exc.code)
    except ValueError as exc:
        raise AgentConfigError(
            "gateway /v1/models returned invalid JSON: %s" % exc)
    except Exception as exc:
        raise AgentConfigError(
            "cannot reach the gateway /v1/models: %s" % exc)
    models = []
    seen = set()
    for item in (payload.get("data") or []):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entry = {"id": mid}
        context = item.get("context_length") or item.get("max_input_tokens")
        if context:
            try:
                entry["context_window"] = int(context)
            except (TypeError, ValueError):
                pass
        out_tokens = item.get("max_output_tokens")
        if out_tokens:
            try:
                entry["max_output"] = int(out_tokens)
            except (TypeError, ValueError):
                pass
        name = item.get("name")
        if name:
            entry["name"] = str(name)
        models.append(entry)
    return models[:80]


# ---------------------------------------------------------------------------
# 独立 CLI（客户端装在别的机器时：改**本机**配置，只从网关取模型表）
# ---------------------------------------------------------------------------

def _default_state_dir():
    return os.path.join(_resolve_home(), ".qoder-agent-config")


def _mask_key(key):
    key = str(key or "")
    if len(key) <= 12:
        return "*" * len(key)
    return key[:6] + "..." + key[-4:]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    state_dir = os.environ.get("QODER_AGENT_STATE") or _default_state_dir()
    if cmd == "list":
        info = overview(state_dir)
        for c in info["clients"]:
            mark = ("[configured]" if c["configured"]
                    else "[installed]" if c["installed"] else "[absent]")
            print("%-12s %-28s %s" % (c["id"], mark, c["label"]))
        return 0
    if cmd in ("plan", "apply", "restore"):
        args = dict(zip(argv[1::2], argv[2::2]))
        client_id = args.get("--client")
        if not client_id:
            print("usage: %s --client <id> [--base-url URL] [--key KEY] "
                  "[--model M] [--no-models]" % cmd)
            return 2
        if cmd == "restore":
            try:
                result = restore(state_dir, client_id)
            except AgentConfigError as exc:
                print("[ERROR] %s" % exc)
                return 1
            for r in result["restored"]:
                print("  %-40s %s" % (r["path"], r["action"]))
            print("restored %s" % result["label"])
            return 0
        base_url = args.get("--base-url") or os.environ.get("QODER_BASE_URL")
        api_key = args.get("--key") or os.environ.get(CREDENTIAL_REF)
        model = args.get("--model")
        if not base_url or not api_key:
            print("[ERROR] --base-url and --key are required "
                  "(or env QODER_BASE_URL / %s)" % CREDENTIAL_REF)
            return 2
        models = []
        if "--no-models" not in args:
            try:
                models = fetch_models_from_gateway(base_url, api_key)
                print("fetched %d models from the gateway" % len(models))
            except AgentConfigError as exc:
                print("[WARN] %s (continuing without model list; "
                      "use --no-models to silence)" % exc)
        if cmd == "plan":
            result = plan(client_id, base_url, api_key, model=model,
                          models=models)
            for f in result["files"]:
                print("=== %s (existed: %s) ===" % (f["path"], f["existed"]))
                print(f["content"])
            return 0
        result = integrate(state_dir, client_id, base_url, api_key,
                           model=model, models=models)
        print("applied %s at %s" % (result["label"], result["applied_at"]))
        for f in result["files"]:
            print("  %-40s %d bytes (existed: %s)"
                  % (f["path"], f["bytes"], f["existed"]))
        return 0
    print("unknown command %r (have: list, plan, apply, restore)" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
