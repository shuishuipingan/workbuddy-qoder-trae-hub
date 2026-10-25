# -*- coding: utf-8 -*-
"""_test_docker_contract.py -- Docker 各处口径必须与注册表一致（第 177-179 轮）。

由来：第 76 轮实跑 docker build 才发现镜像只起两家引擎：
Dockerfile 的 CMD 只写了 --wb-port/--qd-port、HEALTHCHECK 只探两家，
而 EXPOSE 里的 8791 其实是门户端口。三处各写各的，加引擎时必然漏。

第 178 轮补数值一致性；第 179 轮补**compose 环境与卷**：
数据根写在哪、卷挂到哪，必须与 Dockerfile 的 ENV 同一个值 ——
不然容器起来数据落在镜像层，一重建就没了（这正是 7.3 节警告过的事）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOT = P.REPO
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
COMPOSE = os.path.join(ROOT, "docker-compose.yml")


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def env_port(text, key):
    m = re.search(r"^\s*%s=(\d+)" % key, text, re.M)
    return int(m.group(1)) if m else None


def env_value(text, key):
    m = re.search(r"^\s*%s=([^\s\\]+)" % key, text, re.M)
    return m.group(1) if m else None


def cmd_port_list(text, engine):
    return [int(m.group(1)) for m in re.finditer(r"--%s-port\s+(\d+)" % engine, text)]


def all_cmd_ports(text):
    return dict((m.group(1), int(m.group(2)))
                for m in re.finditer(r"--([a-z]+)-port\s+(\d+)", text))


def healthcheck_keys(text):
    return set(re.findall(r"\('([A-Z_]+)',", text))


def expose_ports(text):
    m = re.search(r"^EXPOSE\s+(.+)$", text, re.M)
    return set(int(x) for x in m.group(1).split()) if m else set()


def compose_map(text):
    out = {}
    pat = r'-\s*"\$\{([A-Z_]+):-([0-9]+)\}:([0-9]+)"'
    for m in re.finditer(pat, text):
        out[int(m.group(3))] = (m.group(1), int(m.group(2)))
    for m in re.finditer(r'-\s*"(\d+):(\d+)"', text):
        out.setdefault(int(m.group(2)), (None, int(m.group(1))))
    return out


def compose_env(text):
    """compose 的 environment 列表 -> {KEY: VALUE}（支持 - K=V 与注释行）。"""
    out = {}
    for m in re.finditer(r"^\s*-\s*([A-Z_][A-Z0-9_]*)=([^\s#]+)", text, re.M):
        out[m.group(1)] = m.group(2)
    return out


def compose_volumes(text):
    """compose 的 volumes -> [(host, container)]。"""
    out = []
    for m in re.finditer(r"^\s*-\s*([^:\s]+):(/[^\s:]+)", text, re.M):
        out.append((m.group(1), m.group(2)))
    return out


def host_ports(text):
    """compose ports 里的宿主机端口（含变量默认值）。"""
    out = []
    for m in re.finditer(r'^\s*-\s*"\$\{[A-Z_]+:-([0-9]+)\}:([0-9]+)"', text, re.M):
        out.append((int(m.group(1)), int(m.group(2))))
    for m in re.finditer(r'^\s*-\s*"(\d+):(\d+)"', text, re.M):
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def missing_service_keys(text):
    """compose 服务体里必须有的几项。"""
    need = []
    if not re.search(r"^\s{4}restart:\s*\S", text, re.M):
        need.append("restart")
    if not re.search(r"^\s{4}healthcheck:\s*$", text, re.M):
        need.append("healthcheck")
    else:
        for k in ("interval", "timeout", "retries"):
            if not re.search(r"^\s{6}%s:\s*\S" % k, text, re.M):
                need.append("healthcheck." + k)
    return need


def check_all(engine_names, dockerfile, compose):
    problems = []
    df_ports = {}
    for e in engine_names:
        p = env_port(dockerfile, e.upper() + "_PORT")
        if p is None:
            problems.append("Dockerfile 缺 %s_PORT" % e.upper())
            continue
        df_ports[e] = p
    hub_port = env_port(dockerfile, "HUB_PORT")
    if hub_port is None:
        problems.append("Dockerfile 缺 HUB_PORT")

    used = set(all_cmd_ports(dockerfile))
    for e in engine_names:
        if e not in used:
            problems.append("Dockerfile CMD 没有 --%s-port" % e)
            continue
        vals = cmd_port_list(dockerfile, e)
        want = df_ports.get(e)
        off = [v for v in vals if v != want]
        if off:
            problems.append("CMD 里 --%s-port 有 %s，与 ENV %s_PORT=%s 不一致"
                            % (e, off, e.upper(), want))
    if "hub" in used:
        problems.append("Dockerfile CMD 出现了不存在的 --hub-port")

    expose = expose_ports(dockerfile)
    need = set(df_ports.values())
    if hub_port:
        need.add(hub_port)
    missing = need - expose
    if missing:
        problems.append("EXPOSE 缺端口 %s" % sorted(missing))

    keys = healthcheck_keys(dockerfile)
    for e in engine_names:
        k = e.upper() + "_PORT"
        if k not in keys:
            problems.append("HEALTHCHECK 没探 %s" % k)
    if "HUB_PORT" not in keys:
        problems.append("HEALTHCHECK 没探 HUB_PORT")

    used_c = set(all_cmd_ports(compose))
    for e in engine_names:
        if e not in used_c:
            problems.append("compose command 没有 --%s-port" % e)
            continue
        vals = cmd_port_list(compose, e)
        want = df_ports.get(e)
        off = [v for v in vals if v != want]
        if off:
            problems.append("compose command 里 --%s-port 有 %s，与 ENV %s 不一致"
                            % (e, off, want))

    mapped = compose_map(compose)
    for e, p in df_ports.items():
        if p not in mapped:
            problems.append("compose ports 没有映射 %s 的容器端口 %d" % (e, p))
    if hub_port and hub_port not in mapped:
        problems.append("compose ports 没有映射门户端口 %d" % hub_port)

    # ---- 第 179 轮：compose 环境与卷 ----
    df_root = env_value(dockerfile, "HUB_DATA_ROOT")
    if df_root:
        cenv = compose_env(compose)
        got = cenv.get("HUB_DATA_ROOT")
        if got is None:
            problems.append("compose environment 没设 HUB_DATA_ROOT（数据会落进镜像层）")
        elif got != df_root:
            problems.append("compose HUB_DATA_ROOT=%s 与 Dockerfile 的 %s 不一致" % (got, df_root))
        vols = compose_volumes(compose)
        if not any(container == df_root for _host, container in vols):
            problems.append("compose volumes 没有把宿主机目录挂到 %s" % df_root)
    # ---- 第 180 轮：宿主机端口不许撞车；服务必填项要在 ----
    seen = {}
    for host, container in host_ports(compose):
        if host in seen:
            problems.append("compose 宿主机端口 %d 被映射了两次（容器 %d 与 %d）"
                            % (host, seen[host], container))
        seen[host] = container
    for key in missing_service_keys(compose):
        problems.append("compose 服务缺 %s" % key)

    df_tz = env_value(dockerfile, "TZ")
    cenv = compose_env(compose)
    if df_tz and cenv.get("TZ") and cenv["TZ"] != df_tz:
        problems.append("compose TZ=%s 与 Dockerfile 的 %s 不一致" % (cenv["TZ"], df_tz))
    return problems


def copy_sources(dockerfile):
    """Dockerfile 里 COPY 的来源（跳过 --from=builder 那种），**处理续行**。"""
    joined = re.sub(r"\\\n", " ", dockerfile)      # 反斜杠续行先拼成一行
    srcs = []
    for m in re.finditer(r"^COPY\\s+(?!\\s*--from)(.+)$", joined, re.M):
        parts = m.group(1).split()
        if not parts:
            continue
        for p in parts[:-1]:                      # 最后一个是要去的目标路径
            p = p.strip().strip(chr(34)).rstrip("/")
            if p.startswith("./"):
                p = p[2:]
            if p and p not in (".", "/"):
                srcs.append(p)
    return srcs

def ignore_lines(text):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        out.append(s.rstrip("/"))
    return out


def is_ignored(path, patterns):
    """够用的子集：精确、目录前缀、以及 **/ 前缀的通配。"""
    import fnmatch
    for p in patterns:
        if p == path or path.startswith(p + "/"):
            return True
        if p.startswith("**/"):
            tail = p[3:]
            if fnmatch.fnmatch(path, tail) or fnmatch.fnmatch(path.split("/")[-1], tail):
                return True
        elif fnmatch.fnmatch(path, p):
            return True
    return False


def check_dockerignore(dockerfile, ignore_text, engine_names):
    patterns = ignore_lines(ignore_text)
    problems = []
    # ① COPY 的直接来源不许被排除
    for src in copy_sources(dockerfile):
        if is_ignored(src, patterns):
            problems.append("COPY 源 %s 被 .dockerignore 排除了" % src)
    # ② 引擎目录是运行时必需：COPY hub/ 成功但里面缺一家 = 镜像能构建、跑起来缺模块
    for e in engine_names:
        p = "hub/engines/" + e
        if is_ignored(p, patterns):
            problems.append("引擎目录 %s 被排除了（镜像会缺这家引擎）" % p)
    # ③ 面板静态站
    if is_ignored("admin", patterns):
        problems.append("admin 被排除（镜像里没有面板）")
    return problems


def main():
    if not (os.path.exists(DOCKERFILE) and os.path.exists(COMPOSE)):
        H.skip("D0 Dockerfile / docker-compose.yml 不在（半成品树）", DOCKERFILE)
        H.finish()
        return

    sys.path.insert(0, ROOT)
    from hub import ENGINES
    dockerfile = read(DOCKERFILE)
    compose = read(COMPOSE)

    H.check("D0 注册表可读（引擎名单来自 hub/__init__.py）", len(ENGINES) >= 2, str(ENGINES))

    problems = check_all(ENGINES, dockerfile, compose)
    H.check("D1 Dockerfile / compose 与注册表一致（端口都在且数值相同、数据根与卷对齐）",
            not problems, "；".join(problems[:6]) if problems else "")

    broken = dockerfile.replace("--trae-port 8792", "").replace("('TRAE_PORT','/health'), ", "")
    bad_a = check_all(ENGINES, broken, compose)
    H.check("D2 阳性对照：抽掉 trae 的端口后必须判红", len(bad_a) >= 2,
            "只报了 %d 条：%s" % (len(bad_a), "；".join(bad_a[:3])))

    skew = dockerfile.replace("--trae-port 8792", "--trae-port 8791")
    bad_b = check_all(ENGINES, skew, compose)
    hit = [p for p in bad_b if "不一致" in p]
    H.check("D3 阳性对照：CMD 数值与 ENV 不一致时判红", len(hit) >= 1,
            "没抓到数值不一致：%s" % "；".join(bad_b[:3]))

    # 阳性对照 C：把 compose 的数据根改掉 —— 只有新规则能抓到
    drift = compose.replace("HUB_DATA_ROOT=/app/data", "HUB_DATA_ROOT=/app/other")
    bad_c = check_all(ENGINES, dockerfile, drift)
    hit_c = [p for p in bad_c if "HUB_DATA_ROOT" in p or "volumes" in p]
    H.check("D4 阳性对照：compose 数据根与 Dockerfile 不一致时判红", len(hit_c) >= 1,
            "没抓到数据根漂移：%s" % "；".join(bad_c[:3]))

    # 阳性对照 D/E：追加一条撞车映射 / 去掉 restart
    dup = compose + '\n      - "18791:8799"\n'
    bad_d = check_all(ENGINES, dockerfile, dup)
    hit_d = [p for p in bad_d if "宿主机端口" in p]
    H.check("D5 阳性对照：宿主机端口撞车时判红", len(hit_d) >= 1,
            "没抓到端口冲突：%s" % "；".join(bad_d[:3]))

    nor = compose.replace("    restart: unless-stopped", "")
    bad_e = check_all(ENGINES, dockerfile, nor)
    hit_e = [p for p in bad_e if "缺 restart" in p]
    H.check("D6 阳性对照：缺 restart 时判红", len(hit_e) >= 1,
            "没抓到缺 restart：%s" % "；".join(bad_e[:3]))

    # ---- 第 181 轮：.dockerignore 不许排掉镜像必需的东西 ----
    ignore_text = read(os.path.join(ROOT, ".dockerignore"))
    di = check_dockerignore(dockerfile, ignore_text, ENGINES)
    H.check("D7 .dockerignore 没有排掉镜像必需的内容（COPY 源 / 引擎目录 / admin）",
            not di, "；".join(di[:5]) if di else "")

    bad_ig = ignore_text + "\nhub/engines/trae\n"
    di_bad = check_dockerignore(dockerfile, bad_ig, ENGINES)
    H.check("D8 阳性对照：排掉某家引擎目录时判红",
            any("trae" in p for p in di_bad), "没抓到：%s" % "；".join(di_bad[:3]))

    bad_ig2 = ignore_text + "\nadmin\n"
    di_bad2 = check_dockerignore(dockerfile, bad_ig2, ENGINES)
    H.check("D9 阳性对照：排掉 admin 时判红",
            any("admin" in p for p in di_bad2), "没抓到：%s" % "；".join(di_bad2[:3]))

    H.finish()


main()
