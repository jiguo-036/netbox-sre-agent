# loop.py
"""
NetBox 运维 Agent —— ReAct 循环
Plan → Act(调工具) → Observe(读结果) → 再 Plan …… 直到给出结论

安全边界：最大步数 / 模型调用超时 / 总调用预算 / 重复调用熔断 / 高危操作人工确认

工具只走 registry.py → tools.py 这一条链路：
  - schema 由 TOOL_REGISTRY 自动生成，不在此处手写工具名；
  - 参数校验、高危拦截在 registry 层完成，本层只负责编排与确认闸门。
"""
import os
import time
import json
from pathlib import Path

from dotenv import load_dotenv

# 显式指向项目根的 .env，且必须早于 import registry/tools：
# tools.py 在导入时就会读取 NETBOX_URL / NETBOX_TOKEN，晚加载会导致 token 为空。
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from openai import OpenAI
from registry import DANGEROUS, TOOL_REGISTRY, build_tools_spec, run_tool

MAX_STEPS = 4          # 最大推理步数：模型常不主动收敛，步数多了只会拖慢并重复查询
STEP_TIMEOUT = 30      # 单次模型调用超时（秒）
MAX_TOOL_CALLS = 6     # 总工具调用预算
REPEAT_LIMIT = 1       # 同一工具+同一参数在一轮内最多允许的真实调用次数

# 人工确认词：必须同时点名目标服务，避免“可以帮我看看吗”被误判为确认
CONFIRM_WORDS = ("确认", "同意", "确定", "批准", "confirm", "yes")

SYSTEM_PROMPT = """你是 NetBox 运维排查助手。

工作方式（ReAct）：
- 每次先说明你的计划（Thought），再调用工具（Action），然后读结果（Observation）
- 依据 Observation 决定下一步；信息足够时直接给结论，不要为凑步数乱调工具

安全规则：
1. 只读操作可直接执行；重启、删除、修改类为高危操作，必须先说明风险并取得用户明确同意
2. 不猜测，只依据工具返回的真实数据下结论；查不到就说查不到
3. 同一工具+同一参数在整轮排查中最多调用 2 次；已拿到的信息不要再次查询，
   也不要用「再确认一遍」的理由重复调用，没有新信息就直接给结论
4. 结论要包含：现象、依据（引用具体数据）、建议动作
5. 涉及重启等高危操作时，说明影响范围（NetBox 依赖 redis/postgres，重启可能中断任务）
6. 高危操作采用人工确认闸门：若用户已在当前消息中明确点名目标并确认（如“确认重启 netbox”），
   可直接执行；否则先说明风险，请用户回复“确认重启 <服务名>”，未确认时不得声称已执行
7. 执行前，确认的目标服务必须与用户指定的完全一致；若有任何不确定，停下来询问
8. 一旦容器状态、日志、API 结果已经拿到，立即停止调工具并输出结论；
   不要反复交叉查询同一批服务，步数和工具次数都是有限预算
"""


def _user_confirmed(text: str, target: str) -> bool:
    """当前轮用户消息须同时包含确认词与目标服务名。"""
    t = (text or "").lower()
    if not any(w in t for w in CONFIRM_WORDS):
        return False
    return not target or target.lower() in t


def run_agent(user_input: str, history: list | None = None,
              verbose: bool = True) -> str:
    """
    执行一轮排查。
    history: OpenAI 格式的历史消息列表（不含 system），会被就地追加本轮内容。
    返回值含 [step N] 过程行，前端会把它们折叠成“排查过程”卡片，结论仍在正文。
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return ("❌ 未配置 OPENAI_API_KEY：请在项目根 .env 中填写，"
                "或执行 export OPENAI_API_KEY=sk-xxx")

    client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL"))
    model = os.getenv("MODEL_NAME", "gpt-4o-mini")
    tool_specs = build_tools_spec()
    evidence: list[str] = []   # 成功的观察结果，给强制收尾用精简上下文

    def force_conclusion(reason: str) -> str:
        """预算/步数耗尽时只带精简证据收尾，避免整段历史导致超时。"""
        compact = "\n".join(evidence[-8:]) or "（未取得有效工具数据）"
        followup = [
            {"role": "system", "content":
             "你是 NetBox 运维排查助手。只依据给定证据回答，不要调用工具。"},
            {"role": "user", "content":
             f"用户问题：{user_input}\n\n已获得的工具证据：\n{compact}\n\n"
             f"{reason} 请立即给出最终结论，按「现象 / 依据 / 建议」组织；"
             "数据不足就说明缺什么。"},
        ]
        try:
            rsp = client.chat.completions.create(
                model=model, messages=followup, timeout=90)
            text = rsp.choices[0].message.content or "（无输出）"
        except Exception as e:
            text = f"（生成结论失败：{type(e).__name__}: {e}）"
        msgs.append({"role": "assistant", "content": text})
        return text

    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": user_input})

    t0 = time.time()
    tool_calls = 0
    sig_count: dict[str, int] = {}   # 整轮累计：同一工具+同一参数调用次数
    result = None
    trace: list[str] = []

    try:
        for step in range(1, MAX_STEPS + 1):
            if tool_calls >= MAX_TOOL_CALLS:
                result = force_conclusion(f"已达工具调用预算上限（{MAX_TOOL_CALLS}）。")
                break

            try:
                rsp = client.chat.completions.create(
                    model=model, messages=msgs, tools=tool_specs,
                    tool_choice="auto", timeout=STEP_TIMEOUT)
            except Exception as e:
                result = f"❌ 模型调用失败：{type(e).__name__}: {e}"
                break

            msg = rsp.choices[0].message
            msgs.append(msg)

            if not msg.tool_calls:
                result = msg.content or "（模型未给出内容）"
                break

            for tc in msg.tool_calls:
                fn = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                # 重复调用熔断：整轮累计，防止 A/B/C 轮换绕过「连续相同」检测
                sig = f"{fn}|{json.dumps(args, sort_keys=True)}"
                sig_count[sig] = sig_count.get(sig, 0) + 1

                if sig_count[sig] > REPEAT_LIMIT:
                    obs = {"ok": False,
                           "error": (f"重复调用已熔断：{fn} 同参数本轮已调用 {sig_count[sig]} 次，"
                                     "该结果不会更新，请换方向或直接给出结论")}
                elif fn not in TOOL_REGISTRY:
                    obs = {"ok": False, "error": f"未知工具：{fn}"}
                elif fn in DANGEROUS:
                    target = str(args.get("service") or args.get("container_name") or "")
                    if not _user_confirmed(user_input, target):
                        obs = {"ok": False, "_stage": "confirm_required",
                               "error": (f"{fn} 会中断 {target or '目标服务'}，本次未执行任何变更。"
                                         f"请用户明确回复“确认重启 {target}”后再执行")}
                    else:
                        args["_confirmed"] = True
                        obs = run_tool(fn, args)   # registry 侧再做一次高危校验
                        tool_calls += 1
                else:
                    obs = run_tool(fn, args)
                    tool_calls += 1

                # 关键：必须把工具结果作为 tool 消息回喂，否则模型看不到 Observation
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(obs, ensure_ascii=False)[:1500]})

                step_line = f"[step {step}] {fn} {json.dumps(args, ensure_ascii=False)}"
                out_line = json.dumps(obs, ensure_ascii=False)[:600]
                trace.extend([step_line, out_line])
                if obs.get("ok"):
                    evidence.append(f"{fn} {json.dumps(args, ensure_ascii=False)} -> {out_line[:500]}")
                if verbose:
                    print(f"\n{step_line}")
                    print(out_line)
        else:
            result = force_conclusion(f"已达最大步数 {MAX_STEPS}。")
    finally:
        # 把本轮完整过程（含 tool_calls / tool 结果）回写进 history，支持多轮追问
        if history is not None:
            history.extend(msgs[1:])

    conclusion = result or "（无输出）"
    if verbose:
        print(f"\n耗时 {time.time() - t0:.1f}s，工具调用 {tool_calls} 次")
    # 前端 parseSteps 会把 [step N] + JSON 结果抽成折叠卡片，并从正文过滤掉
    return "\n".join(trace) + "\n\n" + conclusion if trace else conclusion


if __name__ == "__main__":
    hist = []
    print(run_agent("netbox 服务好像有问题，帮我看看", history=hist))
    # 多轮示例：下一句能接住上文
    # print(run_agent("那 postgres 呢", history=hist))
