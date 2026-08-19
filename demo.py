"""
Run questions end to end from the terminal.

    FRESHFLOW_BACKEND=bedrock python demo.py
    python demo.py "your own question"

Prints the same three things the web UI shows -- the answer, every tool call
behind it, and which contested definitions were used -- so a question can be
checked without starting a server.
"""

import sys

from agent import Agent

# The questions Meridian's team actually asks, from the data dictionary.
DEFAULT_QUESTIONS = [
    "What were our top 10 items by shrink last month?",
    "Is shrink up or down versus the prior month?",
    "Which stores have the worst shrink?",
    "Why is dairy shrink up in the Northeast in June?",
    "What are our best-selling items in Produce?",
    "How did strawberry sales trend over the three months?",
    "Which department has the highest shrink rate?",
    "What was our total shrink cost in June?",
    "How do our two banners compare on shrink?",
    "Which items have high unit shrink but little cost impact?",
]

RULE_WIDTH = 78
INDENT = "      "


def print_answer(answer) -> None:
    """The prose, with a rule above it so questions are easy to scroll between."""
    print("\n" + "=" * RULE_WIDTH)
    print("Q:", answer.question)
    print("-" * RULE_WIDTH)
    print(answer.text)


def print_step(step) -> None:
    """One tool call: what ran, the SQL, and what came back."""
    outcome = "ok" if step.ok else "REJECTED"
    print(f"\n  [{outcome}] {step.tool}: {step.purpose}")

    for line in step.sql.strip().splitlines():
        print(INDENT + line.strip())

    if step.ok:
        print(f"{INDENT}-> {len(step.rows)} row(s): {', '.join(step.columns)}")
    else:
        print(f"{INDENT}-> {step.error}")

    for note in step.notes:
        print(f"{INDENT}note: {note}")


def print_definitions(choices) -> None:
    """Which contested definition each answer used, and whether it was chosen."""
    for choice in choices:
        marker = " (default)" if choice["defaulted"] else ""
        print(f"  DEF  {choice['question']} -> {choice['chosen']}{marker}")


def main() -> None:
    # The Windows console is cp1252; a model that writes an em dash would
    # otherwise kill the demo mid-answer.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    agent = Agent()
    print(f"backend: {agent.backend}")

    questions = sys.argv[1:] or DEFAULT_QUESTIONS
    for question in questions:
        answer = agent.ask(question)
        print_answer(answer)

        for step in answer.steps:
            print_step(step)

        print_definitions(answer.choices)

        for warning in answer.warnings:
            print("  !   ", warning)


if __name__ == "__main__":
    main()
