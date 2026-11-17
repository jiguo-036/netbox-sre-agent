# tools.py
"""
NetBox SRE Agent —— 工具层
设计原则：
1. 所有外部命令统一走 _run()，集中做超时、截断、异常兜底
2. 本项目 override 自动加载失效，必须显式 -f 指定基础文件 + override 文件
3. 【关键】显式 -f 会让 compose 用文件路径重推导项目名，导致与已有容器标签
   com.docker.compose.project=netbox-docker 对不上，ps 返回空但退出码 0。
   解法：显式 -f 的同时始终用 -p 锁定项目名。
4. 截断策略必须匹配输出结构类型：
   - 行式流（JSONL / 日志）→ 按行截尾
   - 整体文档（docker inspect 的 pretty JSON）→ 不截断
5. 每个工具返回 dict，字段稳定，方便 LLM 解析
6. 命令成功 ≠ 有结果 ≠ 解析成功，三段要分别断言
7. NetBox 4.x token 有两种格式：v1(nbt 之外) 用 Token 头，v2(nbt_ 开头) 用 Bearer 头
"""

import json
import os
import re
import subprocess
from pathlib import Path

# ---------- 路径与项目名配置 ----------
# 默认取仓库根目录下的 netbox-docker；克隆到任意路径后也可用 NETBOX_DIR 环境变量覆盖
NETBOX_DIR = Path(os.getenv("NETBOX_DIR", str(Path(__file__).resolve().parent.parent / "netbox-docker")))
PROJECT = "netbox-docker"          # 与容器标签 com.docker.compose.project 一致

# 【关键】override 自动加载失效，必须显式声明；顺序即合并顺序，后者覆盖前者
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.override.yml"]

# NetBox API 配置（建议用环境变量注入，别写死在代码里）
NETBOX_URL = os.getenv("NETBOX_URL", "http://localhost:8000")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN", "")


# ---------- 按行截断工具 ----------
def _tail_lines(text, n):
    """按行截取尾部 n 行。绝不按字符切片：会切出残缺 JSON。"""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


# ---------- 统一命令执行入口 ----------
def _run(cmd, timeout=60, cwd=None, mode="lines"):
    """
    mode="lines" : 行式输出（JSONL / 容器日志），按行截尾，安全
    mode="doc"   : 整体文档（如 docker inspect 的 pretty-printed JSON），
                   不截断——切任何边界都会破坏 JSON 结构
    """
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=str(cwd or NETBOX_DIR), shell=False,
        )
        if mode == "doc":
            out, err = p.stdout or "", p.stderr or ""
        else:
            out, err = _tail_lines(p.stdout, 200), _tail_lines(p.stderr, 50)
        return {"code": p.returncode, "stdout": out, "stderr": err}
    except subprocess.TimeoutExpired:
        return {"code": -1, "stdout": "", "stderr": f"命令超时({timeout}s): {' '.join(cmd)}"}
    except Exception as e:
        return {"code": -1, "stdout": "", "stderr": str(e)}


def _compose(extra_args, timeout=60):
    """
    显式 -f 指定基础文件与 override（自动加载在本项目失效）。
    【关键】必须同时带 -p 锁定项目名，否则 -f 会重推导项目上下文，
    导致 ps 返回空但退出码 0。
    """
    cmd = ["docker", "compose"]
    for f in COMPOSE_FILES:
        cmd += ["-f", f]
    cmd += ["-p", PROJECT] + extra_args
    return _run(cmd, timeout=timeout)


# ---------- 健康度解析 ----------
def _parse_health(status_text):
    if not status_text:
        return "unknown"
    m = re.search(r"\((healthy|unhealthy|starting)\)", status_text)
    if m:
        return m.group(1)
    if "Exited" in status_text or "Created" in status_text:
        return "not_running"
    return "unknown"


# ---------- 工具 1：列出容器状态 ----------
def list_services():
    r = _compose(["ps", "-a", "--format", "json"])
    if r["code"] != 0:
        return {"ok": False, "error": r["stderr"] or r["stdout"], "services": []}

    raw = r["stdout"].strip()
    rows = []
    # compose v5.x 输出为 JSONL：一行一个对象
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except json.JSONDecodeError:
            continue

    services = []
    for d in rows:
        status = d.get("Status", "") or ""
        services.append({
            "name": d.get("Name", ""),
            "service": d.get("Service", ""),
            "state": d.get("State", ""),
            "status": status,
            "health": _parse_health(status or d.get("Health", "")),
            "ports": d.get("Ports", "") or d.get("Publishers", ""),
        })

    lines = raw.splitlines()
    return {
        "ok": True,
        "total": len(services),
        "services": services,
        "_debug": {
            "raw_len": len(raw),
            "rows_parsed": len(rows),
            "truncated": len(lines) >= 200,
            "cmd": " ".join(["docker", "compose"]
                            + sum([["-f", f] for f in COMPOSE_FILES], [])
                            + ["-p", PROJECT, "ps", "-a", "--format", "json"]),
            "cwd": str(NETBOX_DIR),
        },
    }


# ---------- 工具 2：查看容器日志 ----------
def get_logs(service, tail=100):
    if not re.match(r"^[a-zA-Z0-9_-]+$", service):
        return {"ok": False, "error": f"非法服务名: {service}"}
    r = _compose(["logs", "--no-color", "--tail", str(int(tail)), service], timeout=90)
    return {"ok": r["code"] == 0, "service": service, "lines": tail,
            "log": r["stdout"] or r["stderr"]}


# ---------- 工具 3：查看单个容器详情 ----------
def inspect_container(container_name):
    if not re.match(r"^[a-zA-Z0-9_-]+$", container_name):
        return {"ok": False, "error": f"非法容器名: {container_name}"}
    # 【关键】docker inspect 输出是整体 JSON 文档，必须用 mode="doc" 不截断
    r = _run(["docker", "inspect", container_name], timeout=30, mode="doc")
    if r["code"] != 0:
        return {"ok": False, "error": r["stderr"]}
    try:
        parsed = json.loads(r["stdout"])
        info = parsed[0] if isinstance(parsed, list) else parsed
    except (json.JSONDecodeError, IndexError) as e:
        return {"ok": False, "error": f"解析 inspect 输出失败: {e}",
                "_debug": {"stdout_len": len(r["stdout"]),
                           "head": r["stdout"][:200]}}

    state = info.get("State", {}) or {}
    health = state.get("Health", {}) or {}
    return {
        "ok": True, "name": container_name,
        "running": state.get("Running", False), "status": state.get("Status", ""),
        "exit_code": state.get("ExitCode", None), "error_msg": state.get("Error", "") or "",
        "health_log": [h.get("Output", "") for h in health.get("Log", [])][-3:],
        "started_at": state.get("StartedAt", ""),
        "ports": info.get("NetworkSettings", {}).get("Ports", {}),
    }


# ---------- 工具 4：NetBox API 探活 ----------
def _auth_header(token):
    """
    【关键】NetBox 4.x 两种 token 格式，认证头不同：
      - v2 token：以 nbt_ 开头 → Authorization: Bearer nbt_xxx
      - v1 token：其余格式     → Authorization: Token xxx
    用错头会返回 403 {"detail":"Invalid v1 token"}，这不是权限问题。
    """
    if token.startswith("nbt_"):
        return f"Bearer {token}"
    return f"Token {token}"


def api_probe(path="/api/"):
    if not NETBOX_TOKEN:
        return {"ok": False, "error": "未设置 NETBOX_TOKEN 环境变量"}

    header = _auth_header(NETBOX_TOKEN)
    r = _run(["curl", "-s", "-i", "--max-time", "10",
              "-H", f"Authorization: {header}", f"{NETBOX_URL}{path}"],
             timeout=15, cwd="/tmp")
    out = r["stdout"]
    status = None
    m = re.match(r"HTTP/[\d.]+\s+(\d+)", out)
    if m:
        status = int(m.group(1))

    # 403 且提示 Invalid v1 token → 大概率认证头选错，给出明确指引
    hint = "正常"
    if status in (401, 403):
        if "Invalid v1 token" in out:
            hint = ("当前 token 是旧版 v1 格式（不以 nbt_ 开头），NetBox 4.7 已不再支持；"
                    "请在 Web 界面重新生成 v2 Token（nbt_ 开头）并更新 .env 的 NETBOX_TOKEN")
        else:
            hint = "token 无效或权限不足，重建 superuser token 并设 write_enabled=True"
    elif status is None:
        hint = "连接失败 → 检查 8000:8080 端口映射是否生效"

    return {
        "ok": status == 200, "url": f"{NETBOX_URL}{path}", "http_status": status,
        "auth_scheme": header.split()[0], "token_prefix": NETBOX_TOKEN[:4],
        "body": out[-1500:], "hint": hint,
    }


# ---------- 工具 5：重启服务 ----------
def restart_service(service):
    if not re.match(r"^[a-zA-Z0-9_-]+$", service):
        return {"ok": False, "error": f"非法服务名: {service}"}
    r = _compose(["restart", service], timeout=120)
    return {"ok": r["code"] == 0, "service": service, "stderr": r["stderr"][:500]}


# ---------- 工具注册表（供 registry.py / loop.py 使用） ----------
TOOL_REGISTRY = {
    "list_services": {"func": list_services,
                      "description": "列出所有服务容器的名称、状态、健康度、端口", "params": {}},
    "get_logs": {"func": get_logs, "description": "获取指定服务最近的日志",
                 "params": {"service": "服务名，如 netbox/postgres/redis", "tail": "行数，默认100"}},
    "inspect_container": {"func": inspect_container,
                          "description": "查看容器详情，包括退出码、健康检查日志、端口绑定",
                          "params": {"container_name": "容器名，如 netbox-docker-netbox-1"}},
    "api_probe": {"func": api_probe, "description": "调用 NetBox API 验证服务是否真正可用",
                  "params": {"path": "API 路径，默认 /api/"}},
    "restart_service": {"func": restart_service, "description": "重启指定服务（不应用配置变更）",
                        "params": {"service": "服务名"}},
}


def dispatch(tool_name, **kwargs):
    entry = TOOL_REGISTRY.get(tool_name)
    if not entry:
        return {"ok": False, "error": f"未知工具: {tool_name}"}
    try:
        return entry["func"](**kwargs)
    except Exception as e:
        return {"ok": False, "error": f"{tool_name} 执行异常: {e}"}


if __name__ == "__main__":
    print("=== list_services ===")
    print(json.dumps(list_services(), ensure_ascii=False, indent=2))
    print("\n=== api_probe ===")
    print(json.dumps(api_probe(), ensure_ascii=False, indent=2))
