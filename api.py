"""
HTTP layer. Thin on purpose -- it holds no logic of its own.

    python api.py                              # http://localhost:8501
    FRESHFLOW_BACKEND=bedrock python api.py

In development Vite serves the UI on 5173 and proxies /api here. Once
`npm run build` has run, this process serves the built UI too, so the demo is
one command on one port.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import metrics as M
from agent import MAX_STEPS, Agent

PORT = int(os.environ.get("PORT", "8501"))
DIST = Path(__file__).parent / "web" / "dist"

EXAMPLES = [
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

app = FastAPI(title="FreshFlow")

# Vite's dev server is a different origin. In the built app nothing crosses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)

# Built once. Loading the CSVs per request would put seconds in front of every
# question.
agent = Agent()


class Question(BaseModel):
    question: str


@app.get("/api/meta")
def meta() -> dict:
    start, end = agent.sem.date_range()
    return {
        "backend": agent.backend,
        "coverage": {"start": start, "end": end},
        "columns": agent.sem.columns(),
        "metrics": M.METRICS,
        "stores": len(agent.sem.values_of("store_id")),
        "departments": agent.sem.values_of("dept"),
        "regions": agent.sem.values_of("region"),
        "banners": agent.sem.values_of("banner"),
        "max_steps": MAX_STEPS,
        "examples": EXAMPLES,
    }


@app.post("/api/ask")
def ask(body: Question) -> dict:
    """
    One question in, one answer plus every query it ran back.

    Stateless by design: no conversation memory, so an answer never depends on
    something the user cannot see in front of them.
    """
    question = (body.question or "").strip()
    if not question:
        return {"error": "Ask a question about shrink or sales."}
    return asdict(agent.ask(question))


if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        candidate = DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    print(f"backend: {agent.backend}")
    if not DIST.is_dir():
        print("web/dist not built. Run `npm run build` in web/, or `npm run dev`.")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
