# app.py
"""
NetBox 运维助手 Web 入口
启动：uvicorn app:app --host 0.0.0.0 --port 8001
访问：http://服务器IP:8001
"""
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loop import run_agent

app = FastAPI(title="NetBox SRE Agent")
INDEX = Path(__file__).parent / "templates" / "index.html"

# 进程内会话存储：{session_id: [OpenAI 格式消息]}
_SESSIONS: dict[str, list] = {}


@app.get("/", response_class=HTMLResponse)
async def home():
    return INDEX.read_text(encoding="utf-8")


@app.post("/chat")
async def chat(req: Request):
    body = await req.json()
    text = (body.get("text") or "").strip()
    sid = body.get("session_id") or str(uuid.uuid4())

    if not text:
        return JSONResponse({"error": "消息为空"}, status_code=400)

    # 取出（或初始化）该会话的历史，run_agent 会就地追加
    history = _SESSIONS.setdefault(sid, [])

    t0 = time.time()
    try:
        import anyio
        reply = await anyio.to_thread.run_sync(
            lambda: run_agent(text, history=history))
    except Exception as e:
        reply = f"❌ 调用失败：{type(e).__name__}: {e}"

    return {"session_id": sid, "reply": reply,
            "elapsed": round(time.time() - t0, 1)}


@app.post("/reset")
async def reset(req: Request):
    """清空指定会话上下文"""
    body = await req.json()
    sid = body.get("session_id")
    if sid in _SESSIONS:
        _SESSIONS[sid].clear()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True)
