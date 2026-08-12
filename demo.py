"""
Run questions end to end from the terminal.

    FRESHFLOW_BACKEND=bedrock python demo.py
    python demo.py "your own question"
"""

import sys

from agent import Agent

# The questions Meridian's team actually asks, from the data dictionary.
QUESTIONS = [
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

# The Windows console is cp1252; a model that writes an em dash would otherwise
# kill the demo mid-answer.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

agent = Agent()
print(f"backend: {agent.backend}")

for question in sys.argv[1:] or QUESTIONS:
    answer = agent.ask(question)
    print("\n" + "=" * 78)
    print("Q:", question)
    print("-" * 78)
    print(answer.text)

    for step in answer.steps:
        flag = "ok" if step.ok else "REJECTED"
        print(f"\n  [{flag}] {step.purpose}")
        for line in step.sql.strip().splitlines():
            print("      " + line.strip())
        if step.ok:
            print(f"      -> {len(step.rows)} row(s): {', '.join(step.columns)}")
        else:
            print(f"      -> {step.error}")

    for choice in answer.choices:
        mark = " (default)" if choice["defaulted"] else ""
        print(f"  DEF  {choice['question']} -> {choice['chosen']}{mark}")

    for warning in answer.warnings:
        print("  !   ", warning)
