#!/usr/bin/env python3
"""
Combine per-skill eval reports into a single consolidated report.

Reads all *-report.json files written by runner.py and produces:
  combined-report.json  — machine-readable aggregate
  combined-report.html  — visual HTML report (score bars, pass/fail badges,
                          collapsible per-skill case tables)

Usage:
  python evals/combine.py
  python evals/combine.py --artifacts-dir /tmp/out
  python evals/combine.py --artifacts-dir /tmp/out --output /tmp/combined
"""

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def combine_reports(artifacts_dir: Path) -> dict:
    report_files = sorted(
        f for f in artifacts_dir.glob("*-report.json")
        if f.name != "combined-report.json"
    )

    if not report_files:
        print(
            f"No *-report.json files found in {artifacts_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    skill_reports: list[dict] = [
        json.loads(f.read_text(encoding="utf-8")) for f in report_files
    ]

    all_cases = [
        case
        for report in skill_reports
        for case in report.get("cases", [])
    ]
    scoreable = [
        c for c in all_cases
        if "error" not in c and c.get("avg_score") is not None
    ]

    def _avg(seq):
        return round(sum(seq) / len(seq), 1) if seq else None

    overall_pass_rate = (
        round(sum(c["pass_rate"] for c in scoreable) / len(scoreable), 3)
        if scoreable else None
    )
    overall_avg_score    = _avg([c["avg_score"] for c in scoreable])
    overall_avg_tokens   = (
        round(sum(c["avg_input_tokens"] + c["avg_output_tokens"] for c in scoreable) / len(scoreable))
        if scoreable else None
    )
    overall_avg_duration = _avg([c["avg_duration_ms"] for c in scoreable])

    failing_cases: list[dict] = []
    for case in scoreable:
        if case["pass_rate"] >= 1.0:
            continue
        last_trial = case["trials"][-1] if case.get("trials") else {}
        det_failures = [
            chk["detail"]
            for chk in (last_trial.get("deterministic_checks") or {}).get("results", [])
            if not chk["passed"]
        ]
        grader_issues = (last_trial.get("grade") or {}).get("issues") or []
        failing_cases.append({
            "skill": case["skill"],
            "id": case["id"],
            "prompt": case["prompt"],
            "pass_rate": case["pass_rate"],
            "avg_score": case["avg_score"],
            "deterministic_failures": det_failures,
            "grader_issues": grader_issues,
        })

    error_cases = [
        {"skill": c["skill"], "id": c["id"], "prompt": c["prompt"], "error": c["error"]}
        for c in all_cases
        if "error" in c
    ]

    providers = sorted({r.get("provider") for r in skill_reports if r.get("provider")})
    models    = sorted({r.get("model")    for r in skill_reports if r.get("model")})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(artifacts_dir),
        "providers": providers,
        "models": models,
        "skills_evaluated": len(skill_reports),
        "total_cases": len(all_cases),
        "scoreable_cases": len(scoreable),
        "overall": {
            "pass_rate": overall_pass_rate,
            "avg_score": overall_avg_score,
            "avg_tokens": overall_avg_tokens,
            "avg_duration_ms": overall_avg_duration,
        },
        "skills": [
            {
                "skill": r["skill"],
                "provider": r.get("provider"),
                "model": r.get("model"),
                "generated_at": r.get("generated_at"),
                "trials_per_case": r.get("trials_per_case"),
                "summary": r.get("summary"),
                "cases": r.get("cases", []),
            }
            for r in skill_reports
        ],
        "failing_cases": failing_cases,
        "error_cases": error_cases,
    }


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def _score_color(score) -> str:
    """Map a 0-10 score to a CSS color class."""
    if score is None:
        return "score-na"
    if score >= 8:
        return "score-high"
    if score >= 6:
        return "score-mid"
    return "score-low"


def _pass_badge(pass_rate) -> str:
    if pass_rate is None:
        return '<span class="badge badge-na">N/A</span>'
    pct = int(pass_rate * 100)
    if pct == 100:
        return '<span class="badge badge-pass">PASS 100%</span>'
    if pct == 0:
        return '<span class="badge badge-fail">FAIL 0%</span>'
    return f'<span class="badge badge-partial">PARTIAL {pct}%</span>'


def _score_bar(score) -> str:
    if score is None:
        return '<div class="score-bar-wrap"><span class="score-na-text">N/A</span></div>'
    pct = score * 10
    cls = _score_color(score)
    return (
        f'<div class="score-bar-wrap">'
        f'<div class="score-bar {cls}" style="width:{pct}%"></div>'
        f'<span class="score-label">{score}/10</span>'
        f'</div>'
    )


def _e(text) -> str:
    return html.escape(str(text))


def _render_skill_table(skill: dict) -> str:
    cases = skill.get("cases", [])
    if not cases:
        return "<p>No cases.</p>"

    rows = []
    for c in cases:
        score = c.get("avg_score")
        pass_rate = c.get("pass_rate")
        tok = (c.get("avg_input_tokens") or 0) + (c.get("avg_output_tokens") or 0)
        tok_str = f"{tok/1000:.1f}k" if tok >= 1000 else str(int(tok))
        dur = c.get("avg_duration_ms")
        dur_str = f"{dur/1000:.1f}s" if dur is not None else "—"

        # Issues from last trial
        last_trial = c.get("trials", [{}])[-1]
        det_failures = [
            chk["detail"]
            for chk in (last_trial.get("deterministic_checks") or {}).get("results", [])
            if not chk["passed"]
        ]
        grader_issues = (last_trial.get("grade") or {}).get("issues") or []
        all_issues = det_failures + grader_issues

        issue_html = ""
        if all_issues:
            items = "".join(f"<li>{_e(i)}</li>" for i in all_issues)
            issue_html = f'<ul class="issues">{items}</ul>'

        error_html = f'<div class="error-msg">{_e(c["error"])}</div>' if "error" in c else ""

        rows.append(
            f"<tr>"
            f'<td class="case-id">{_e(c.get("id", ""))}</td>'
            f'<td class="case-prompt">{_e(c.get("prompt", ""))}{error_html}</td>'
            f'<td>{_score_bar(score)}</td>'
            f'<td>{_pass_badge(pass_rate)}</td>'
            f'<td class="mono">{tok_str}</td>'
            f'<td class="mono">{dur_str}</td>'
            f'<td>{issue_html}</td>'
            f"</tr>"
        )

    return (
        '<table class="case-table">'
        "<thead><tr>"
        "<th>ID</th><th>Prompt</th><th>Score</th>"
        "<th>Pass</th><th>Tokens</th><th>Latency</th><th>Issues</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_html(report: dict) -> str:
    overall   = report["overall"]
    generated = report["generated_at"].replace("T", " ").split(".")[0] + " UTC"
    providers = ", ".join(report.get("providers") or ["—"])
    models    = ", ".join(report.get("models") or ["—"])

    overall_score    = overall.get("avg_score")
    overall_pass_pct = round((overall.get("pass_rate") or 0) * 100, 1)
    overall_tokens   = overall.get("avg_tokens") or "—"
    overall_dur      = overall.get("avg_duration_ms")
    overall_dur_str  = f"{overall_dur/1000:.1f}s" if overall_dur else "—"

    # ── skill summary cards ───────────────────────────────────────────────
    skill_cards = []
    for s in report["skills"]:
        summ     = s.get("summary") or {}
        score    = summ.get("avg_score")
        pr       = summ.get("pass_rate")
        pct      = round((pr or 0) * 100, 1)
        total    = summ.get("total_cases", 0)
        scoreable = summ.get("scoreable_cases", 0)
        tok      = (summ.get("avg_input_tokens") or 0) + (summ.get("avg_output_tokens") or 0)
        tok_str  = f"{tok/1000:.1f}k" if tok >= 1000 else str(int(tok))
        dur      = summ.get("avg_duration_ms")
        dur_str  = f"{dur/1000:.1f}s" if dur else "—"
        sc_cls   = _score_color(score)
        card_cls = "card-pass" if pr == 1.0 else ("card-fail" if (pr or 0) == 0 else "card-partial")

        table_html  = _render_skill_table(s)
        detail_id   = f"detail-{_e(s['skill'])}"

        skill_cards.append(f"""
        <div class="skill-card {card_cls}">
          <div class="skill-header" onclick="toggle('{detail_id}')">
            <span class="skill-name">{_e(s['skill'])}</span>
            <span class="skill-meta">{_e(s.get('provider',''))} / {_e(s.get('model',''))}</span>
            <span class="skill-stats">
              {_score_bar(score)}
              {_pass_badge(pr)}
              <span class="mono stat-pill">cases {scoreable}/{total}</span>
              <span class="mono stat-pill">{tok_str} tok</span>
              <span class="mono stat-pill">{dur_str}</span>
            </span>
            <span class="toggle-arrow" id="arrow-{detail_id}">▶</span>
          </div>
          <div class="skill-detail" id="{detail_id}" style="display:none">
            {table_html}
          </div>
        </div>""")

    # ── failing cases section ─────────────────────────────────────────────
    failing_rows = []
    for fc in report.get("failing_cases", []):
        pct = int(fc["pass_rate"] * 100)
        issues_html = ""
        all_issues = fc.get("deterministic_failures", []) + fc.get("grader_issues", [])
        if all_issues:
            items = "".join(f"<li>{_e(i)}</li>" for i in all_issues)
            issues_html = f'<ul class="issues">{items}</ul>'
        failing_rows.append(
            f"<tr>"
            f'<td class="case-id">{_e(fc["skill"])}<br><small>{_e(fc["id"])}</small></td>'
            f'<td class="case-prompt">{_e(fc["prompt"])}</td>'
            f'<td>{_score_bar(fc.get("avg_score"))}</td>'
            f'<td><span class="badge badge-partial">{pct}%</span></td>'
            f"<td>{issues_html}</td>"
            f"</tr>"
        )

    failing_section = ""
    if failing_rows:
        failing_section = f"""
        <section>
          <h2>Failing Cases</h2>
          <table class="case-table">
            <thead><tr><th>Skill / ID</th><th>Prompt</th><th>Score</th><th>Pass%</th><th>Issues</th></tr></thead>
            <tbody>{''.join(failing_rows)}</tbody>
          </table>
        </section>"""

    # ── error cases section ───────────────────────────────────────────────
    error_section = ""
    if report.get("error_cases"):
        error_rows = "".join(
            f'<tr><td>{_e(ec["skill"])}</td><td>{_e(ec["id"])}</td>'
            f'<td>{_e(ec["prompt"])}</td><td class="error-msg">{_e(ec["error"])}</td></tr>'
            for ec in report["error_cases"]
        )
        error_section = f"""
        <section>
          <h2>Errored Cases</h2>
          <table class="case-table">
            <thead><tr><th>Skill</th><th>ID</th><th>Prompt</th><th>Error</th></tr></thead>
            <tbody>{error_rows}</tbody>
          </table>
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eval Report</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          font-size: 14px; background: #f0f2f5; color: #1a1a2e; }}
  a {{ color: #4f6ef7; }}

  /* ── layout ── */
  .page {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
  header {{ background: #1a1a2e; color: #fff; padding: 24px 32px; border-radius: 10px;
            margin-bottom: 24px; }}
  header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 6px; }}
  header .meta {{ font-size: 12px; color: #a0aec0; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #2d3748; }}

  /* ── overall stats bar ── */
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                 gap: 12px; margin-bottom: 28px; }}
  .stat-box {{ background: #fff; border-radius: 8px; padding: 16px 20px;
               box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .stat-box .label {{ font-size: 11px; color: #718096; text-transform: uppercase;
                      letter-spacing: .05em; margin-bottom: 6px; }}
  .stat-box .value {{ font-size: 26px; font-weight: 700; }}
  .stat-box .value.score-high {{ color: #38a169; }}
  .stat-box .value.score-mid  {{ color: #d69e2e; }}
  .stat-box .value.score-low  {{ color: #e53e3e; }}

  /* ── skill cards ── */
  .skill-card {{ background: #fff; border-radius: 8px; margin-bottom: 10px;
                 box-shadow: 0 1px 3px rgba(0,0,0,.07);
                 border-left: 4px solid #cbd5e0; overflow: hidden; }}
  .skill-card.card-pass    {{ border-left-color: #38a169; }}
  .skill-card.card-partial {{ border-left-color: #d69e2e; }}
  .skill-card.card-fail    {{ border-left-color: #e53e3e; }}

  .skill-header {{ display: flex; align-items: center; gap: 12px; padding: 14px 18px;
                   cursor: pointer; user-select: none; flex-wrap: wrap; }}
  .skill-header:hover {{ background: #f7fafc; }}
  .skill-name {{ font-weight: 600; font-size: 15px; min-width: 160px; }}
  .skill-meta {{ font-size: 11px; color: #718096; min-width: 120px; }}
  .skill-stats {{ display: flex; align-items: center; gap: 8px; flex: 1; flex-wrap: wrap; }}
  .toggle-arrow {{ margin-left: auto; color: #a0aec0; font-size: 12px; transition: transform .2s; }}
  .toggle-arrow.open {{ transform: rotate(90deg); }}

  .skill-detail {{ padding: 0 18px 18px; }}

  /* ── score bar ── */
  .score-bar-wrap {{ display: flex; align-items: center; gap: 6px; min-width: 110px; }}
  .score-bar {{ height: 8px; border-radius: 4px; transition: width .3s; }}
  .score-bar.score-high {{ background: #38a169; }}
  .score-bar.score-mid  {{ background: #d69e2e; }}
  .score-bar.score-low  {{ background: #e53e3e; }}
  .score-bar.score-na   {{ background: #cbd5e0; width: 30% !important; }}
  .score-label {{ font-size: 12px; color: #4a5568; white-space: nowrap; }}
  .score-na-text {{ font-size: 12px; color: #a0aec0; }}

  /* ── badges ── */
  .badge {{ font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px;
            white-space: nowrap; }}
  .badge-pass    {{ background: #c6f6d5; color: #276749; }}
  .badge-partial {{ background: #fefcbf; color: #744210; }}
  .badge-fail    {{ background: #fed7d7; color: #9b2c2c; }}
  .badge-na      {{ background: #e2e8f0; color: #718096; }}

  .stat-pill {{ background: #edf2f7; color: #4a5568; font-size: 11px;
                padding: 2px 7px; border-radius: 10px; white-space: nowrap; }}

  /* ── case table ── */
  .case-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }}
  .case-table th {{ background: #f7fafc; padding: 8px 10px; text-align: left;
                    font-weight: 600; color: #4a5568; border-bottom: 2px solid #e2e8f0;
                    white-space: nowrap; }}
  .case-table td {{ padding: 8px 10px; border-bottom: 1px solid #edf2f7;
                    vertical-align: top; }}
  .case-table tr:last-child td {{ border-bottom: none; }}
  .case-table tr:hover td {{ background: #f7fafc; }}
  .case-id {{ font-weight: 600; white-space: nowrap; color: #4a5568; }}
  .case-prompt {{ color: #2d3748; max-width: 340px; }}
  .mono {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }}

  /* ── issues ── */
  .issues {{ margin: 4px 0 0 0; padding: 0; list-style: none; }}
  .issues li {{ font-size: 12px; color: #744210; padding: 1px 0; }}
  .issues li::before {{ content: "• "; }}
  .error-msg {{ color: #9b2c2c; font-size: 12px; margin-top: 2px; }}

  /* ── footer ── */
  footer {{ text-align: center; font-size: 11px; color: #a0aec0; margin-top: 32px; }}
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Skills Eval Report</h1>
    <div class="meta">
      Generated {_e(generated)} &nbsp;·&nbsp;
      Provider: {_e(providers)} &nbsp;·&nbsp;
      Model: {_e(models)} &nbsp;·&nbsp;
      Source: {_e(report.get('source_dir',''))}
    </div>
  </header>

  <div class="stats-grid">
    <div class="stat-box">
      <div class="label">Avg Score</div>
      <div class="value {_score_color(overall_score)}">{overall_score if overall_score is not None else "N/A"}<span style="font-size:14px;color:#718096">/10</span></div>
    </div>
    <div class="stat-box">
      <div class="label">Pass Rate</div>
      <div class="value {_score_color((overall.get('pass_rate') or 0)*10)}">{overall_pass_pct}<span style="font-size:14px;color:#718096">%</span></div>
    </div>
    <div class="stat-box">
      <div class="label">Skills</div>
      <div class="value" style="color:#4f6ef7">{report['skills_evaluated']}</div>
    </div>
    <div class="stat-box">
      <div class="label">Cases</div>
      <div class="value" style="color:#4f6ef7">{report['scoreable_cases']}<span style="font-size:14px;color:#718096">/{report['total_cases']}</span></div>
    </div>
    <div class="stat-box">
      <div class="label">Avg Tokens</div>
      <div class="value" style="color:#4a5568;font-size:20px">{overall_tokens}</div>
    </div>
    <div class="stat-box">
      <div class="label">Avg Latency</div>
      <div class="value" style="color:#4a5568;font-size:20px">{overall_dur_str}</div>
    </div>
  </div>

  <section>
    <h2>Skills</h2>
    {''.join(skill_cards)}
  </section>

  {failing_section}
  {error_section}

  <footer>Generated by evals/combine.py &nbsp;·&nbsp; {_e(generated)}</footer>
</div>

<script>
  function toggle(id) {{
    const el = document.getElementById(id);
    const arrow = document.getElementById('arrow-' + id);
    const hidden = el.style.display === 'none';
    el.style.display = hidden ? 'block' : 'none';
    arrow.classList.toggle('open', hidden);
  }}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Console printer
# ---------------------------------------------------------------------------

def _print_report(report: dict) -> None:
    overall = report["overall"]
    print(f"\n{'=' * 64}")
    print(
        f"Skills: {report['skills_evaluated']}  "
        f"Cases: {report['total_cases']} ({report['scoreable_cases']} scoreable)"
    )
    print(
        f"Overall: score={overall['avg_score']}/10  "
        f"pass_rate={round((overall['pass_rate'] or 0) * 100, 1)}%  "
        f"avg_tokens={overall['avg_tokens']}  "
        f"avg_ms={overall['avg_duration_ms']}"
    )

    print(f"\n{'─' * 64}")
    print(f"{'Skill':<32}  {'Score':>5}  {'Pass%':>5}  {'Tokens':>7}")
    print(f"{'─' * 64}")
    for s in report["skills"]:
        summ     = s.get("summary") or {}
        score    = summ.get("avg_score")
        pass_pct = round((summ.get("pass_rate") or 0) * 100, 0)
        avg_tok  = (summ.get("avg_input_tokens") or 0) + (summ.get("avg_output_tokens") or 0)
        tok_str  = f"{avg_tok / 1000:.1f}k" if avg_tok >= 1000 else str(int(avg_tok))
        print(f"  {s['skill']:<30}  {str(score) + '/10':>5}  {str(int(pass_pct)) + '%':>5}  {tok_str:>7}")

    if report["failing_cases"]:
        print(f"\n{'─' * 64}")
        print("Failing cases:")
        for fc in report["failing_cases"]:
            pct = int(fc["pass_rate"] * 100)
            preview = fc["prompt"][:55] + "..." if len(fc["prompt"]) > 58 else fc["prompt"]
            print(f"  [{fc['skill']} / {fc['id']}] pass={pct}%  score={fc['avg_score']}/10")
            print(f"    {preview}")
            for d in fc.get("deterministic_failures", []):
                print(f"      ✗ {d}")
            for issue in fc.get("grader_issues", []):
                print(f"      • {issue}")

    if report["error_cases"]:
        print(f"\n{'─' * 64}")
        print("Errored cases:")
        for ec in report["error_cases"]:
            print(f"  [{ec['skill']} / {ec['id']}] {ec['error']}")

    print(f"{'=' * 64}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Combine per-skill eval reports into JSON + HTML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python evals/combine.py\n"
            "  python evals/combine.py --artifacts-dir /tmp/out\n"
            "  python evals/combine.py --output /tmp/combined\n"
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        metavar="PATH",
        default=str(Path(__file__).parent / "artifacts"),
        help="Directory containing *-report.json files (default: evals/artifacts/).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "Base output path (no extension). "
            "Produces <path>.json and <path>.html. "
            "Defaults to <artifacts-dir>/combined-report."
        ),
    )
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    if not artifacts_dir.exists():
        print(f"Error: artifacts dir not found: {artifacts_dir}", file=sys.stderr)
        sys.exit(1)

    report = combine_reports(artifacts_dir)

    base = Path(args.output) if args.output else artifacts_dir / "combined-report"
    json_path = base.with_suffix(".json")
    html_path = base.with_suffix(".html")

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")

    _print_report(report)
    print(f"JSON report → {json_path}")
    print(f"HTML report → {html_path}")


if __name__ == "__main__":
    main()
