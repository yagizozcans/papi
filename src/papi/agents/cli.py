"""``papi agent`` — agent base system: list, inspect, run."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from papi.agents.definition import AgentError, list_agents, load_agent
from papi.agents.registry import discover
from papi.agents.runner import allowed_tools, run_agent
from papi.core.db import connect
from papi.core.jobs import Deferred

app = typer.Typer(help="Ajan altyapısı: beceriler, araçlar, çalıştırma.", no_args_is_help=True)
console = Console(soft_wrap=True, markup=False, highlight=False)


@app.command("list")
def list_() -> None:
    """Agents, their skills and bound tools (also validates every definition)."""
    for name in list_agents():
        try:
            a = load_agent(name)
        except (AgentError, ValueError) as exc:
            console.print(f"{name}: GEÇERSİZ — {exc}")
            continue
        console.print(
            f"{a.name} v{a.agent_version} · {a.model} · ≤{a.max_turns} tur · ≤${a.max_budget_usd:g}/çalışma"
        )
        for s in a.skills:
            console.print(f"  skill {s.name}: {', '.join(s.allowed_tools) or '-'}")


@app.command()
def tools() -> None:
    """Every registered tool (one per papi/agents/tools module)."""
    for t in discover().values():
        console.print(f"{t.name} [{t.effect}] — {t.description.splitlines()[0]}")


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Agent name, e.g. kap-analyst, strategy-analyst.")],
    ref: Annotated[str, typer.Argument(help="Run subject: KAP disclosure number, thesis name, …")],
    priority: Annotated[str | None, typer.Option(help="Governor priority (default: the agent's).")] = None,
    show_calls: Annotated[bool, typer.Option("--calls/--no-calls", help="Print the tool-call log.")] = True,
    run_id: Annotated[str | None, typer.Option(help="Run id to use (the dashboard passes its own).")] = None,
) -> None:
    """Run an agent on one subject and print its answer, tool calls and cost."""
    try:
        a = load_agent(agent)
    except (AgentError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    prompt = a.prompt.format(ref=ref)
    with connect() as conn:
        try:
            r = run_agent(conn, a, prompt, subject=ref, run_id=run_id, priority=priority)
        except Deferred as exc:
            console.print(f"ertelendi: {exc}")
            raise typer.Exit(1) from exc
    res = r.result
    if show_calls:
        for c in r.tool_calls():
            ok = c["result"].get("success", True)
            console.print(
                f"  {c['ms']:>5} ms  {c['tool']}({json.dumps(c['args'], ensure_ascii=False)}) "
                f"→ {'ok' if ok else 'HATA'}"
            )
    u = r.usage
    console.print(
        f"run {r.run_id} · {r.meta.get('status')} · {r.num_turns} tur"
        f"{' (yeniden başlatıldı)' if r.resumed else ''} · ${r.cost_usd:.4f} · "
        f"cache okuma {u.get('cache_read_input_tokens', 0)} / "
        f"yazma {u.get('cache_creation_input_tokens', 0)} / girdi {u.get('input_tokens', 0)} token"
    )
    console.print(f"transcript: {r.transcript_path} · izinli araçlar: {len(allowed_tools(a))}")
    if res.structured_output is not None:
        console.print(json.dumps(res.structured_output, ensure_ascii=False, indent=2))
    elif res.text:
        console.print(res.text)
    if r.gate:
        console.print(f"TAMAMLANMADI: {r.gate}")
    elif not res.ok:
        console.print(f"HATA: {res.error_kind}: {res.error}")
    if not r.ok:
        raise typer.Exit(1)
