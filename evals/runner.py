#!/usr/bin/env python3
"""
Skills Eval Runner — central template.

Discovers skills by finding SKILL.md + evals/prompts.json under the repo's
.github/skills/ tree, runs each prompt through Claude with mock tool responses,
and grades each response against a shared rubric.

Approach (per https://developers.openai.com/blog/eval-skills and
https://medium.com/google-cloud/agent-skills-evals-stop-vibe-testing-your-skills):
  prompt → [N trials] → agentic run → deterministic checks → rubric grader → score

Per-skill contract (two required files, one optional):
  .github/skills/<name>/SKILL.md            — instructions as system prompt
  .github/skills/<name>/evals/prompts.json  — test cases
  .github/skills/<name>/evals/tools.py      — optional; skill-specific mock
                                              tool definitions and responses.
                                              Must export:
                                                DEFINITIONS: list[dict]
                                                get_response(name, input) -> dict
                                              Falls back to _NoTools (no-op).

Tool definitions in tools.py must use Anthropic format (input_schema).
The runner converts them automatically for OpenAI.

Each prompt in prompts.json may include an optional "checks" object:
  {
    "must_call":     ["tool_a", "tool_b"],   -- these tools must appear in the trace
    "must_not_call": ["tool_c"]              -- these tools must NOT appear
  }
  These are evaluated deterministically before the model grader is invoked.

Usage:
  python evals/runner.py
  python evals/runner.py --provider openai
  python evals/runner.py --provider anthropic --model claude-opus-4-7
  python evals/runner.py --skill my-skill
  python evals/runner.py --skill a --skill b
  python evals/runner.py --trials 3
  python evals/runner.py --root /path/to/repo --artifacts-dir /tmp/out

Requires: ANTHROPIC_API_KEY (anthropic) or OPENAI_API_KEY (openai)
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

# ---------------------------------------------------------------------------
# Load sibling modules via importlib so the runner works regardless of the
# caller's working directory or PYTHONPATH.
# ---------------------------------------------------------------------------
_EVALS_DIR = Path(__file__).parent


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_grader = _load_module("grader", _EVALS_DIR / "grader.py")


class _NoTools:
    """Fallback used when a skill provides no evals/tools.py."""
    DEFINITIONS: list = []

    @staticmethod
    def get_response(name: str, tool_input: dict) -> dict:  # noqa: ARG004
        return {
            "error": f"No tools.py found for this skill; '{name}' unavailable."
        }


MAX_TURNS = 10
LOOP_WARN_THRESHOLD = 6  # turns above this are flagged as potential thrashing
PASS_THRESHOLD = 7        # overall_score >= this is considered passing

PROVIDER_DEFAULTS: dict[str, dict] = {
    "anthropic": {"model": "claude-sonnet-4-6", "env_var": "ANTHROPIC_API_KEY"},
    "openai":    {"model": "gpt-4o",            "env_var": "OPENAI_API_KEY"},
}


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------

def _create_llm(provider: str, model: str, max_tokens: int):
    if provider == "anthropic":
        return ChatAnthropic(model=model, max_tokens=max_tokens)
    if provider == "openai":
        return ChatOpenAI(model=model, max_tokens=max_tokens)
    raise ValueError(f"Unknown provider: {provider!r}")


def _convert_tools(tool_defs: list[dict], provider: str) -> list[dict]:
    """Convert Anthropic-format tool defs to the provider's expected format."""
    if provider != "openai":
        return tool_defs
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tool_defs
    ]


def _normalize_stop_reason(response) -> str:
    """Return a unified stop-reason string across providers.

    Anthropic uses 'stop_reason'; OpenAI uses 'finish_reason'.
    """
    meta = response.response_metadata
    return meta.get("stop_reason") or meta.get("finish_reason") or ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_skills(root: Path) -> list[Path]:
    """
    Return sorted skill directories that contain both SKILL.md and
    evals/prompts.json — the two files every skill must provide.
    """
    skills_root = root / ".github" / "skills"
    return sorted(
        p.parent.parent
        for p in skills_root.glob("*/evals/prompts.json")
        if (p.parent.parent / "SKILL.md").exists()
    )


def _load_skill(skill_dir: Path) -> tuple[str, list[dict]]:
    instructions = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    prompts = json.loads(
        (skill_dir / "evals" / "prompts.json").read_text(encoding="utf-8")
    )
    return instructions, prompts


def _load_tools(skill_dir: Path) -> ModuleType:
    """
    Load the skill's evals/tools.py if present, else return _NoTools.
    Module must expose DEFINITIONS (list[dict]) and
    get_response(name: str, input: dict) -> dict.
    """
    skill_tools_path = skill_dir / "evals" / "tools.py"
    if skill_tools_path.exists():
        return _load_module("skill_tools", skill_tools_path)
    return _NoTools  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Deterministic checks  (fast, model-free, immune to hallucinations)
# ---------------------------------------------------------------------------

def _run_deterministic_checks(row: dict, run_result: dict) -> dict:
    """
    Evaluate the declarative 'checks' field from a prompt row against the
    tool calls captured in run_result.

    Supported check types:
      must_call     — every listed tool must appear at least once in the trace
      must_not_call — none of the listed tools may appear in the trace

    Returns:
      passed  — True only when every individual check passes
      results — list of {check, passed, detail} dicts
    """
    checks = row.get("checks", {})
    tools_called = run_result["tools_called"]
    results = []

    for tool in checks.get("must_call", []):
        passed = tool in tools_called
        results.append({
            "check": f"must_call:{tool}",
            "passed": passed,
            "detail": (
                f"'{tool}' found in trace"
                if passed
                else f"'{tool}' was expected but never called"
            ),
        })

    for tool in checks.get("must_not_call", []):
        passed = tool not in tools_called
        results.append({
            "check": f"must_not_call:{tool}",
            "passed": passed,
            "detail": (
                f"'{tool}' correctly absent"
                if passed
                else f"'{tool}' was called but must not be"
            ),
        })

    return {
        "passed": all(r["passed"] for r in results) if results else True,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Agentic execution loop
# ---------------------------------------------------------------------------

def _run_case(
    llm,
    provider: str,
    skill_instructions: str,
    prompt: str,
    tools: ModuleType,
) -> dict:
    """
    Run a single prompt through the LLM with the skill instructions as the
    system prompt and the provided tools module for mock responses.

    Returns:
      final_text     — last assistant text joined into one string
      tools_called   — tool names in call order
      turn_count     — number of agentic turns used
      input_tokens   — total input tokens across all turns
      output_tokens  — total output tokens across all turns
      events         — full JSONL-serialisable trace (one entry per turn)
    """
    system_prompt = (
        "You are an AI assistant with access to external tools via an MCP Server. "
        "Follow the skill instructions below exactly, including input-gathering "
        "rules and output format requirements.\n\n"
        + skill_instructions
    )

    tool_defs = _convert_tools(tools.DEFINITIONS, provider)
    active_llm = llm.bind_tools(tool_defs) if tool_defs else llm

    messages: list = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=prompt),
    ]
    events: list[dict] = []
    last_response = None
    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(MAX_TURNS):
        response = active_llm.invoke(messages)
        last_response = response

        meta = response.response_metadata
        in_tok = meta.get("usage", {}).get("input_tokens", 0)
        out_tok = meta.get("usage", {}).get("output_tokens", 0)
        # OpenAI nests usage differently
        if not in_tok and not out_tok:
            usage = meta.get("token_usage", {})
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
        total_input_tokens += in_tok
        total_output_tokens += out_tok

        content = response.content
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        stop_reason = _normalize_stop_reason(response)
        events.append({
            "turn": turn,
            "stop_reason": stop_reason,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "content": content,
        })

        # "tool_use" = Anthropic, "tool_calls" = OpenAI
        if stop_reason not in ("tool_use", "tool_calls"):
            break

        tool_calls = response.tool_calls
        if not tool_calls:
            break

        tool_results = [
            ToolMessage(
                content=json.dumps(tools.get_response(tc["name"], tc["args"])),
                tool_call_id=tc["id"],
                name=tc["name"],
            )
            for tc in tool_calls
        ]
        messages.append(response)
        messages.extend(tool_results)

    final_text_parts = []
    if last_response is not None:
        content = last_response.content
        if isinstance(content, str):
            final_text_parts.append(content)
        else:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    final_text_parts.append(block["text"])
    final_text = "\n".join(final_text_parts)

    tools_called = [
        block["name"]
        for event in events
        for block in event["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]

    return {
        "final_text": final_text,
        "tools_called": tools_called,
        "turn_count": len(events),
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Per-skill orchestration
# ---------------------------------------------------------------------------

def run_skill(
    llm,
    provider: str,
    model: str,
    skill_dir: Path,
    artifacts_dir: Path,
    trials: int = 1,
) -> list[dict]:
    skill_name = skill_dir.name
    skill_instructions, prompts = _load_skill(skill_dir)
    tools = _load_tools(skill_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 64}")
    print(f"Skill: {skill_name}  ({len(prompts)} cases, {trials} trial(s) each)")
    print("=" * 64)

    results: list[dict] = []

    for row in prompts:
        preview = (
            row["prompt"][:46] + "..."
            if len(row["prompt"]) > 49
            else row["prompt"]
        )
        print(f"  [{row['id']}] {preview:<49}", end="", flush=True)

        trial_records: list[dict] = []
        error = None

        try:
            for t in range(trials):
                t_start = time.perf_counter()
                run_result = _run_case(
                    llm, provider, skill_instructions, row["prompt"], tools
                )
                duration_ms = int((time.perf_counter() - t_start) * 1000)

                det = _run_deterministic_checks(row, run_result)
                grade_result = _grader.grade(
                    llm, provider, skill_name, skill_instructions, row, run_result
                )

                score = (
                    grade_result.get("overall_score")
                    if grade_result
                    else None
                )
                passed = (
                    isinstance(score, (int, float))
                    and score >= PASS_THRESHOLD
                    and det["passed"]
                )
                loop_flag = run_result["turn_count"] > LOOP_WARN_THRESHOLD

                trial_records.append({
                    "trial": t + 1,
                    "tools_called": run_result["tools_called"],
                    "turn_count": run_result["turn_count"],
                    "loop_detected": loop_flag,
                    "input_tokens": run_result["input_tokens"],
                    "output_tokens": run_result["output_tokens"],
                    "duration_ms": duration_ms,
                    "deterministic_checks": det,
                    "grade": grade_result,
                    "passed": passed,
                })

                # Save per-trial trace
                trace_path = (
                    artifacts_dir
                    / f"{skill_name}-{row['id']}-t{t + 1}.jsonl"
                )
                trace_path.write_text(
                    "\n".join(json.dumps(e) for e in run_result["events"]),
                    encoding="utf-8",
                )

        except Exception as exc:
            error = str(exc)

        # Aggregate across trials
        passing_trials = [tr for tr in trial_records if tr["passed"]]
        scored_trials = [
            tr for tr in trial_records
            if isinstance(
                (tr["grade"] or {}).get("overall_score"), (int, float)
            )
        ]

        def _avg(key: str) -> float:
            vals = [tr[key] for tr in trial_records if key in tr]
            return round(sum(vals) / len(vals), 1) if vals else 0.0

        result = {
            "skill": skill_name,
            **{k: v for k, v in row.items() if k != "checks"},
            "checks_defined": row.get("checks", {}),
            "pass_rate": (
                len(passing_trials) / len(trial_records)
                if trial_records else 0.0
            ),
            "avg_score": (
                round(
                    sum(
                        tr["grade"]["overall_score"]
                        for tr in scored_trials
                    ) / len(scored_trials),
                    1,
                )
                if scored_trials else None
            ),
            "avg_input_tokens": _avg("input_tokens"),
            "avg_output_tokens": _avg("output_tokens"),
            "avg_duration_ms": _avg("duration_ms"),
            "trials": trial_records,
            **({"error": error} if error else {}),
        }
        results.append(result)

        # Save aggregated result
        grade_path = artifacts_dir / f"{skill_name}-{row['id']}.grade.json"
        grade_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

        # Console output
        if error:
            print(f"ERROR: {error}")
        else:
            pass_count = len(passing_trials)
            avg_score = result["avg_score"] or "?"
            tok = result["avg_input_tokens"] + result["avg_output_tokens"]
            tok_str = f"{tok / 1000:.1f}k" if tok >= 1000 else str(int(tok))
            loops = sum(1 for tr in trial_records if tr["loop_detected"])
            loop_str = f"  ⚠ LOOP x{loops}" if loops else ""
            if trials == 1:
                det = trial_records[0]["deterministic_checks"]
                det_str = "✓" if det["passed"] else "✗"
                print(
                    f"det={det_str}  score={avg_score}/10  "
                    f"{'PASS' if pass_count else 'FAIL'}  "
                    f"tok={tok_str}{loop_str}"
                )
            else:
                print(
                    f"pass={pass_count}/{trials}  avg={avg_score}/10  "
                    f"tok={tok_str}{loop_str}"
                )

    _print_summary(results, trials)

    # Per-skill report
    valid = [r for r in results if "error" not in r and r.get("avg_score") is not None]
    skill_report = {
        "skill": skill_name,
        "provider": provider,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trials_per_case": trials,
        "summary": {
            "total_cases": len(results),
            "scoreable_cases": len(valid),
            "pass_rate": round(sum(r["pass_rate"] for r in valid) / len(valid), 3) if valid else None,
            "avg_score": round(sum(r["avg_score"] for r in valid) / len(valid), 1) if valid else None,
            "avg_input_tokens": round(sum(r["avg_input_tokens"] for r in valid) / len(valid), 1) if valid else None,
            "avg_output_tokens": round(sum(r["avg_output_tokens"] for r in valid) / len(valid), 1) if valid else None,
            "avg_duration_ms": round(sum(r["avg_duration_ms"] for r in valid) / len(valid), 1) if valid else None,
        },
        "cases": results,
    }
    report_path = artifacts_dir / f"{skill_name}-report.json"
    report_path.write_text(json.dumps(skill_report, indent=2), encoding="utf-8")

    return results


def _print_summary(results: list[dict], trials: int) -> None:
    valid = [r for r in results if "error" not in r and r["avg_score"] is not None]
    if not valid:
        print("\n  No scoreable results.")
        return

    avg_score = round(sum(r["avg_score"] for r in valid) / len(valid), 1)
    avg_pass_rate = round(
        sum(r["pass_rate"] for r in valid) / len(valid) * 100, 1
    )
    avg_tok = round(
        sum(r["avg_input_tokens"] + r["avg_output_tokens"] for r in valid)
        / len(valid)
    )

    failing = [r for r in valid if r["pass_rate"] < 1.0]

    print(
        f"\n  avg_score={avg_score}/10  pass_rate={avg_pass_rate}%  "
        f"avg_tokens={avg_tok}"
    )
    if failing:
        print("  Failing / partial cases:")
        for r in failing:
            pct = int(r["pass_rate"] * 100)
            print(
                f"    [{r['id']}] pass={pct}%  avg={r['avg_score']}/10"
                f"  \"{r['prompt']}\""
            )
            # Show deterministic failures from last trial
            last = r["trials"][-1] if r["trials"] else {}
            for chk in (last.get("deterministic_checks") or {}).get("results", []):
                if not chk["passed"]:
                    print(f"          ✗ {chk['detail']}")
            # Show model-grader issues from last trial
            grade = last.get("grade") or {}
            for issue in grade.get("issues") or []:
                print(f"          • {issue}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run evals for Claude Code skills.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python evals/runner.py\n"
            "  python evals/runner.py --provider openai\n"
            "  python evals/runner.py --provider anthropic --model claude-opus-4-7\n"
            "  python evals/runner.py --skill my-skill\n"
            "  python evals/runner.py --trials 3\n"
            "  python evals/runner.py --root /repo --artifacts-dir /tmp/out\n"
        ),
    )
    parser.add_argument(
        "--provider",
        choices=list(PROVIDER_DEFAULTS),
        default="anthropic",
        help="LLM provider to use (default: anthropic).",
    )
    _provider_defaults_str = ", ".join(
        f"{p}: {d['model']}" for p, d in PROVIDER_DEFAULTS.items()
    )
    parser.add_argument(
        "--model",
        metavar="NAME",
        default=None,
        help=(
            "Model name override. "
            f"Defaults to the provider's default ({_provider_defaults_str})."
        ),
    )
    parser.add_argument(
        "--skill",
        metavar="NAME",
        action="append",
        dest="skills",
        help=(
            "Skill name to evaluate (repeatable). "
            "Defaults to all discovered skills."
        ),
    )
    parser.add_argument(
        "--trials",
        metavar="N",
        type=int,
        default=1,
        help=(
            "Number of times to run each prompt (default: 1). "
            "Set to 3-5 for production confidence."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=str(Path(__file__).parent.parent),
        help=(
            "Repo root that contains .github/skills/. "
            "Defaults to the parent directory of evals/."
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        metavar="PATH",
        default=str(Path(__file__).parent / "artifacts"),
        help=(
            "Directory for trace and grade output files. "
            "Defaults to evals/artifacts/."
        ),
    )
    args = parser.parse_args()

    if args.trials < 1:
        print("Error: --trials must be at least 1.", file=sys.stderr)
        sys.exit(1)

    provider = args.provider
    required_env = PROVIDER_DEFAULTS[provider]["env_var"]
    if not os.environ.get(required_env):
        print(
            f"Error: {required_env} environment variable is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    model = args.model or PROVIDER_DEFAULTS[provider]["model"]
    root = Path(args.root)
    artifacts_dir = Path(args.artifacts_dir)
    llm = _create_llm(provider, model, max_tokens=2048)

    all_skill_dirs = discover_skills(root)
    if not all_skill_dirs:
        print(
            f"No skills found under {root / '.github' / 'skills'}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.skills:
        name_to_dir = {d.name: d for d in all_skill_dirs}
        unknown = [s for s in args.skills if s not in name_to_dir]
        if unknown:
            print(f"Unknown skill(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"Available: {', '.join(name_to_dir)}", file=sys.stderr)
            sys.exit(1)
        skill_dirs = [name_to_dir[s] for s in args.skills]
    else:
        skill_dirs = all_skill_dirs

    print(f"Provider: {provider}  Model: {model}")

    all_results: list[dict] = []
    for skill_dir in skill_dirs:
        results = run_skill(llm, provider, model, skill_dir, artifacts_dir, args.trials)
        all_results.extend(results)

    summary_path = artifacts_dir / "summary.json"
    summary_path.write_text(
        json.dumps(all_results, indent=2), encoding="utf-8"
    )

    # Grand totals
    all_valid = [
        r for r in all_results
        if "error" not in r and r.get("avg_score") is not None
    ]
    grand_avg = (
        round(sum(r["avg_score"] for r in all_valid) / len(all_valid), 1)
        if all_valid else "N/A"
    )
    grand_pass_pct = (
        round(
            sum(r["pass_rate"] for r in all_valid) / len(all_valid) * 100, 1
        )
        if all_valid else "N/A"
    )
    grand_avg_tok = (
        round(
            sum(
                r["avg_input_tokens"] + r["avg_output_tokens"]
                for r in all_valid
            ) / len(all_valid)
        )
        if all_valid else "N/A"
    )

    print(f"\n{'=' * 64}")
    print(
        f"Overall: avg_score={grand_avg}/10  pass_rate={grand_pass_pct}%  "
        f"avg_tokens={grand_avg_tok}"
    )
    print(f"Results written to {summary_path}")
    print("=" * 64)


if __name__ == "__main__":
    main()
