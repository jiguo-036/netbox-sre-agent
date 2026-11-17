# registry.py
"""
工具注册表 → LLM 可读的 JSON Schema
设计原则：
1. 单一事实来源：schema 从 tools.TOOL_REGISTRY 派生，不手写工具名和描述，
   否则加了新工具忘了改 schema，模型会调不到 / 调到不存在的工具。
2. 参数必须显式声明类型：tail 若不给 integer，模型传 "abc" 会直接炸穿主循环。
3. 有副作用的工具打 dangerous 标记，由本层拦截，绝不让模型"顺手试一下"。
4. 校验放在这层，tools.py 只负责命令执行，不承担防御职责。
"""

import json
from tools import TOOL_REGISTRY, dispatch

# ---------- 参数级约束（tools.py 的 PARAMS 表达不了的东西） ----------
PARAM_SPEC = {
    "service":        {"type": "string"},
    "container_name": {"type": "string"},
    "path":           {"type": "string", "default": "/api/"},
    "tail":           {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
}

# 有副作用、需要人工确认的工具
DANGEROUS = {"restart_service"}


def build_tools_spec() -> list[dict]:
    """生成 OpenAI / DashScope 兼容的 tools 数组，直接塞进 chat 请求。"""
    specs = []
    for name, entry in TOOL_REGISTRY.items():
        props, required = {}, []
        for pname, pdesc in (entry.get("params") or {}).items():
            spec = dict(PARAM_SPEC.get(pname, {"type": "string"}))
            spec["description"] = pdesc
            props[pname] = spec
            if "default" not in spec:
                required.append(pname)

        fn = {
            "name": name,
            "description": (entry["description"]
                            + ("【高危：会中断服务，调用前必须向用户确认】"
                               if name in DANGEROUS else "")),
            "parameters": {"type": "object", "properties": props,
                           "required": required},
        }
        specs.append({"type": "function", "function": fn})
    return specs


def coerce_and_validate(tool_name: str, args: dict) -> tuple[dict, str | None]:
    """返回 (清洗后的参数, 错误信息)。错误非 None 时不执行，回喂给模型自纠。"""
    entry = TOOL_REGISTRY.get(tool_name)
    if not entry:
        return args, f"未知工具: {tool_name}，可用工具: {list(TOOL_REGISTRY)}"

    allowed = set(entry.get("params") or {})
    out, errs = {}, []

    for k, v in args.items():
        if k == "_confirmed":
            # 人工确认标记由 loop.py 在用户明确同意后注入，不作为模型可见参数，
            # 因此不进入 PARAM_SPEC，但要放行到 run_tool 的高危闸门
            out[k] = v
            continue
        if k not in allowed:
            errs.append(f"参数 {k} 不被 {tool_name} 接受，允许的参数: {sorted(allowed) or '无'}")
            continue
        spec = PARAM_SPEC.get(k, {"type": "string"})
        if spec["type"] == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError):
                errs.append(f"参数 {k} 需为整数，收到: {v!r}")
                continue
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if lo is not None and v < lo:
                v = lo            # 越界钳制而非报错，比拒绝省事
            if hi is not None and v > hi:
                v = hi
        out[k] = v

    for k, spec in PARAM_SPEC.items():
        if k in allowed and k not in out and "default" in spec:
            out[k] = spec["default"]

    for k in allowed:
        if k not in out and "default" not in PARAM_SPEC.get(k, {}):
            errs.append(f"缺少必填参数 {k}（{entry['params'].get(k, '')}）")

    return out, ("; ".join(errs) if errs else None)


def run_tool(tool_name: str, args: dict | None = None) -> dict:
    """loop.py 的唯一执行入口：校验 → 高危拦截 → 执行。"""
    clean, err = coerce_and_validate(tool_name, args or {})
    if err:
        return {"ok": False, "error": f"参数错误: {err}", "_stage": "validate"}
    if tool_name in DANGEROUS and not clean.get("_confirmed"):
        clean.pop("_confirmed", None)
        return {"ok": False, "_stage": "confirm_required",
                "error": f"{tool_name} 有中断服务的副作用，需用户明确同意后再调用"}
    clean.pop("_confirmed", None)
    return dispatch(tool_name, **clean)


if __name__ == "__main__":
    specs = build_tools_spec()
    print(f"共 {len(specs)} 个工具，与注册表一致: {len(specs) == len(TOOL_REGISTRY)}")
    print(json.dumps(specs, ensure_ascii=False, indent=2))

    print("\n--- 校验自测 ---")
    for name, a in [("get_logs", {"service": "netbox", "tail": "20"}),
                    ("get_logs", {"service": "netbox", "tail": "abc"}),
                    ("get_logs", {}),
                    ("list_services", {"foo": 1}),
                    ("restart_service", {"service": "netbox"})]:
        _, e = coerce_and_validate(name, a)
        print(f"{name} {a} -> {e or 'OK'}")
