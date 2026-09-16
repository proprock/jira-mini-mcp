"""Score how reliably a model picks the right jira-mini-mcp tool.

Deliberately outside the default test suite: this calls a real model, so it
needs credentials and spends money. `uv run pytest` stays offline.

    uv run python evals/run_eval.py

The tool definitions come from the real server, not a copy, so the prompt the
model sees is the one a host would send. Nothing here touches Jira: the
scenarios stop at the choice of tool and its arguments, which is the part a
small toolset is supposed to make reliable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import Message, ToolParam, Usage
from mcp import Client

from jira_mini_mcp.server import create_server

SCENARIOS = Path(__file__).parent / "scenarios.json"

DEFAULT_MODEL = "claude-opus-5"

# Deliberately thin. The tool descriptions are what is under test, so the
# system prompt must not do their job for them by hinting at a choice.
SYSTEM = (
    "You are a coding agent working on a Jira ticket. Use the available tools "
    "to do what the developer asks. Call exactly one tool."
)

# Synthetic: the server only needs to start to advertise its tools, and
# nothing in this script issues a Jira request.
_SYNTHETIC_ENV = {
    "JIRA_BASE_URL": "https://synthetic-tenant.atlassian.net",
    "JIRA_EMAIL": "agent@example.com",
    "JIRA_API_TOKEN": "not-a-real-token",
}


@dataclass
class Outcome:
    scenario_id: str
    expected: str
    chosen: str | None
    arguments: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


async def tool_definitions() -> list[ToolParam]:
    """The nine tools exactly as the server advertises them."""
    previous = {key: os.environ.get(key) for key in _SYNTHETIC_ENV}
    os.environ.update(_SYNTHETIC_ENV)
    read_only = os.environ.pop("READ_ONLY_MODE", None)
    try:
        async with Client(create_server()) as client:
            tools = (await client.list_tools()).tools
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if read_only is not None:
            os.environ["READ_ONLY_MODE"] = read_only

    return [
        ToolParam(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.input_schema,
        )
        for tool in tools
    ]


def grade(scenario: dict[str, Any], chosen: str | None, arguments: dict[str, Any]) -> Outcome:
    expect = scenario["expect"]
    outcome = Outcome(
        scenario_id=scenario["id"],
        expected=expect["tool"],
        chosen=chosen,
        arguments=arguments,
    )

    if chosen is None:
        outcome.failures.append("called no tool")
        return outcome
    if chosen != expect["tool"]:
        outcome.failures.append(f"chose {chosen}")
        return outcome

    for name, value in expect.get("arguments", {}).items():
        if arguments.get(name) != value:
            outcome.failures.append(f"{name}={arguments.get(name)!r}, wanted {value!r}")
    for name in expect.get("has_arguments", []):
        if name not in arguments:
            outcome.failures.append(f"missing {name}")
    for name in expect.get("lacks_arguments", []):
        if name in arguments:
            outcome.failures.append(f"set {name}, which it should not")
    return outcome


def run_scenario(
    client: anthropic.Anthropic,
    model: str,
    tools: list[ToolParam],
    scenario: dict[str, Any],
) -> tuple[Outcome, Usage]:
    response: Message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM,
        tools=tools,
        messages=[{"role": "user", "content": scenario["prompt"]}],
    )

    # tool_choice stays on its default: a model that answers in prose instead
    # of calling anything has made a selection error worth recording, and
    # forcing a call would hide it.
    call = next((block for block in response.content if block.type == "tool_use"), None)
    chosen = call.name if call is not None else None
    arguments = dict(call.input) if call is not None and isinstance(call.input, dict) else {}
    return grade(scenario, chosen, arguments), response.usage


def report(outcomes: list[Outcome], usage: dict[str, int], model: str) -> None:
    width = max(len(outcome.scenario_id) for outcome in outcomes)
    print()
    for outcome in outcomes:
        mark = "pass" if outcome.passed else "FAIL"
        detail = "" if outcome.passed else "  <- " + "; ".join(outcome.failures)
        print(f"  {mark}  {outcome.scenario_id:<{width}}  {outcome.expected}{detail}")

    passed = sum(1 for outcome in outcomes if outcome.passed)
    total = len(outcomes)
    print(f"\n  {passed}/{total} correct ({passed / total:.0%}) on {model}")
    print(f"  tokens: {usage['input']} in, {usage['output']} out across {total} requests\n")
    print("  A failure is a signal about the tool descriptions, not only about")
    print("  the model: read what the chosen tool's description promised, and")
    print("  whether the right tool's description said the thing that would")
    print("  have distinguished it.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--only", help="run one scenario by id")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    scenarios = payload["scenarios"]
    if args.only:
        scenarios = [item for item in scenarios if item["id"] == args.only]
        if not scenarios:
            raise SystemExit(f"no scenario with id {args.only!r}")

    tools = asyncio.run(tool_definitions())
    client = anthropic.Anthropic()

    outcomes: list[Outcome] = []
    usage = {"input": 0, "output": 0}
    for scenario in scenarios:
        outcome, scenario_usage = run_scenario(client, args.model, tools, scenario)
        outcomes.append(outcome)
        usage["input"] += scenario_usage.input_tokens
        usage["output"] += scenario_usage.output_tokens

    if args.json:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "usage": usage,
                    "results": [
                        {
                            "id": outcome.scenario_id,
                            "expected": outcome.expected,
                            "chosen": outcome.chosen,
                            "arguments": outcome.arguments,
                            "failures": outcome.failures,
                        }
                        for outcome in outcomes
                    ],
                },
                indent=2,
            )
        )
    else:
        report(outcomes, usage, args.model)

    return 0 if all(outcome.passed for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
