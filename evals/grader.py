"""
Model-assisted rubric grader.

Central template — skill-agnostic. Accepts the skill instructions and run
result from the runner and returns a structured grade dict.
"""

import json
from pathlib import Path

import anthropic

MODEL = "claude-sonnet-4-6"

RUBRIC_SCHEMA = json.loads(
    (Path(__file__).parent / "rubric.schema.json").read_text(encoding="utf-8")
)


def grade(
    client: anthropic.Anthropic,
    skill_name: str,
    skill_instructions: str,
    row: dict,
    run_result: dict,
) -> dict | None:
    """
    Grade a single skill run against the shared rubric.

    Returns the grade dict (matching rubric.schema.json properties) or None
    if the model failed to return a structured response.
    """
    is_negative = row.get("skill_should_activate") is False
    tools_called_str = " → ".join(run_result["tools_called"]) or "none"
    negative_note = (
        "This is a NEGATIVE test case — the skill should NOT have activated or performed "
        "the requested action. Grade whether it correctly declined.\n"
        if is_negative
        else ""
    )

    grading_prompt = (
        f'You are an evaluator grading an AI assistant\'s response to a test case for '
        f'the "{skill_name}" skill.\n\n'
        f"## Skill Instructions\n{skill_instructions}\n\n"
        f"## Test Case\n"
        f"- Prompt: \"{row['prompt']}\"\n"
        f"- Case type: {row.get('case_type', '')}\n"
        f"- Skill should activate: {row.get('skill_should_activate', '')}\n"
        f"- Notes: {row.get('notes', '')}\n\n"
        f"## Response Under Evaluation\n"
        f"Tools called (in order): {tools_called_str}\n\n"
        f"Final text response:\n{run_result['final_text']}\n\n"
        f"## Instructions\n"
        f"{negative_note}"
        f"Score the response using the submit_grade tool. "
        f"Set negative_case_respected to null for positive test cases."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": grading_prompt}],
        tools=[
            {
                "name": "submit_grade",
                "description": "Submit the structured evaluation grade for this skill response.",
                "input_schema": {
                    "type": "object",
                    "required": RUBRIC_SCHEMA["required"],
                    "properties": RUBRIC_SCHEMA["properties"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "submit_grade"},
    )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    return tool_use.input if tool_use else None
