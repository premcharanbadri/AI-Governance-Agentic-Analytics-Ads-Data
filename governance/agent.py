"""Tool-using analytics agent: a local Qwen model (Ollama) that reaches data only through the MCP server.

The agent never connects to the warehouse. It sees only the tools the MCP server lists for its role, and every
tool call is checked and logged by the policy gateway inside that server.

Usage:
  python governance/agent.py "How many orders did we have in August 2026?"
  python governance/agent.py --role data_engineer "..."
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway import load_config  # noqa: E402

SYSTEM_PROMPT = """You are the analytics assistant for Lumen Goods, a U.S. home and kitchen brand.
Rules:
- Answer only from tool results. Never invent, estimate or recall numbers.
- Before computing a metric, call resolve_metric to get its certified definition. If a metric is not
  certified, say so and do not estimate it.
- State the definition you used, the date range, and any data-freshness caveats.
- If a result says SUPPRESSED, say the group is too small to report. Do not give a number or zero for it.
- If a tool call is denied, explain that it is not allowed. Do not try to work around a denial.
- You cannot change data. Requests to change data go to request_data_change for human approval.
- Personal data (emails, names, phone numbers, addresses, ZIP-level detail on small groups) is never shared.
- Dates in tool arguments are YYYY-MM-DD. The data runs to 2026-09-30.
- If the question gives no time period, pick a full period (for example the latest complete month, or 2026
  year to date) and say which one you used. Never present a single day as an overall total.
- Do not guess reasons or causes that the tool results do not show. If no data matched, say that; do not
  speculate about why.
- Only say something was denied if a tool result said so. If you could not answer, say what was missing.
Keep answers short and factual."""


class OllamaBackend:
    def __init__(self, model: str, temperature: float = 0):
        import ollama
        self.client = ollama.Client(host=os.environ.get("OLLAMA_HOST"))
        self.model, self.temperature = model, temperature

    def describe(self) -> dict:
        try:
            info = self.client.show(self.model)
            details = getattr(info, "details", None)
            return {"model": self.model, "digest": getattr(info, "digest", None) or "see `ollama list`",
                    "quantization": getattr(details, "quantization_level", None),
                    "parameters": getattr(details, "parameter_size", None)}
        except Exception as e:      # model not pulled, server not running
            return {"model": self.model, "error": str(e)}

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        resp = self.client.chat(model=self.model, messages=messages, tools=tools, think=False,
                                options={"temperature": self.temperature})
        msg = resp.message
        calls = [{"name": c.function.name, "arguments": dict(c.function.arguments or {})}
                 for c in (msg.tool_calls or [])]
        return {"content": msg.content or "", "tool_calls": calls}


class ScriptedBackend:
    """Replays fixed tool calls, then a fixed answer. Used to test the plumbing without a model."""

    def __init__(self, script: list[dict]):
        self.script, self.i = script, 0

    def describe(self) -> dict:
        return {"model": "scripted (no LLM)"}

    def chat(self, messages, tools) -> dict:
        step = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return {"content": step.get("content", ""), "tool_calls": step.get("tool_calls", [])}


def _mcp_tools_to_ollama(tools) -> list[dict]:
    out = []
    for t in tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object"}
        out.append({"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                     "parameters": schema}})
    return out


async def run_agent(question: str, role: str, backend, max_steps: int = 6, session_id: str | None = None) -> dict:
    """Answer one question. Returns the answer, every tool call with its result, and the session id."""
    session_id = session_id or uuid.uuid4().hex[:12]
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "governance" / "mcp_server.py")],
                                   env={**os.environ, "GOV_ROLE": role, "GOV_SESSION_ID": session_id},
                                   cwd=str(ROOT))
    trace: list[dict] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = _mcp_tools_to_ollama((await mcp.list_tools()).tools)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
            answer, nudged = None, False
            for _ in range(max_steps):
                reply = backend.chat(messages, tools)
                if not reply["tool_calls"]:
                    answer = reply["content"].strip()
                    if answer or nudged:
                        break
                    # Empty reply with no tool call: ask once for an answer, and record that we had to.
                    nudged = True
                    trace.append({"tool": "(agent loop)", "arguments": {}, "result": "empty reply; nudged once"})
                    messages.append({"role": "user", "content": "Your last reply was empty. Answer the question "
                                     "from the tool results, or say what you could not find."})
                messages.append({"role": "assistant", "content": reply["content"],
                                 "tool_calls": [{"function": c} for c in reply["tool_calls"]]})
                for call in reply["tool_calls"]:
                    res = await mcp.call_tool(call["name"], call["arguments"])
                    text = "".join(getattr(c, "text", "") for c in res.content) or "{}"
                    trace.append({"tool": call["name"], "arguments": call["arguments"], "result": text[:4000]})
                    messages.append({"role": "tool", "content": text, "tool_name": call["name"]})
            if not answer:
                answer = ("[empty answer]" if nudged and answer == "" else
                          "[stopped: step limit reached without a final answer]")
    return {"session_id": session_id, "role": role, "question": question, "answer": answer,
            "tool_calls": [t for t in trace if t["tool"] != "(agent loop)"], "nudged": nudged}


def main():
    cfg = load_config()["agent"]
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--role", default="analyst")
    ap.add_argument("--model", default=cfg["model"])
    a = ap.parse_args()
    backend = OllamaBackend(a.model, cfg["temperature"])
    out = asyncio.run(run_agent(a.question, a.role, backend, cfg["max_steps"]))
    for c in out["tool_calls"]:
        print(f"-> {c['tool']}({json.dumps(c['arguments'])})")
    print("\n" + out["answer"])


if __name__ == "__main__":
    main()
