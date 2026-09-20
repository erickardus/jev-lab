"""Village: a tiny top-down world whose people are driven by Jev, watched from a browser.

  uv run examples/village/server.py                # http://127.0.0.1:8765
  uv run examples/village/server.py --narrator template # canned dialogue (default is Claude when ANTHROPIC_API_KEY is set)
  uv run examples/village/server.py --brain random       # no Jev; sanity-check the world itself
  uv run examples/village/server.py --headless 60        # run 60 sim-seconds, print the event log, exit

Code owns the world (map, paths, clock, needs, memory); Jev owns the choices (one
Choice per idle villager, batched into one request per tick); an optional LLM owns
the words when two villagers talk. See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402  (module level: FastAPI must resolve the WebSocket annotation)
from fastapi.responses import FileResponse  # noqa: E402

from sim.brain import JevBrain, RandomBrain  # noqa: E402
from sim.engine import Sim  # noqa: E402
from sim.narrator import ClaudeNarrator, TemplateNarrator  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
BUILD = 4  # bump when the websocket protocol changes; the page warns if the server is older
STEP = 0.05  # sim step in seconds (20 Hz)
BROADCAST_HZ = 10


def make_sim(brain_name: str, narrator_name: str, model: str | None, narrator_model: str | None = None) -> Sim:
    brain = JevBrain(model) if brain_name == "jev" else RandomBrain()
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if narrator_name == "auto":
        narrator_name = "claude" if have_key else "template"
    if narrator_name == "claude" and not have_key:
        print("--narrator claude needs ANTHROPIC_API_KEY; using templates", file=sys.stderr)
        narrator_name = "template"
    narrator = ClaudeNarrator(narrator_model) if narrator_name == "claude" and narrator_model else ClaudeNarrator() if narrator_name == "claude" else TemplateNarrator()
    return Sim(brain, narrator)


async def run_headless(sim: Sim, seconds: float, factor: float = 8.0) -> None:
    """Run `seconds` of sim time at `factor`x real time: fast, but slow enough for the
    brain's requests (a few hundred ms) to land while the world is still moving."""
    steps = int(seconds / STEP)
    for _ in range(steps):
        sim.tick(STEP)
        await asyncio.sleep(STEP / factor)
    if sim._inflight:
        await sim._inflight
    for e in sim.world.chronicle:
        print(f"d{e['day']} {e['hhmm']}  {e['text']}")
    snap = sim.snapshot()
    print(json.dumps({"brain": snap["brain"], "narrator": snap["narrator"], "clock": snap["clock"]}, indent=2))
    for n in sim.npcs:
        print(f"{n.name:<8} {n.action_label:<28} needs={n.snapshot()['needs_words']}  memory={list(n.memory)[-3:]}")


def build_app(sim: Sim):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)
    clients: set[WebSocket] = set()

    async def loop() -> None:
        acc = 0.0
        last = asyncio.get_event_loop().time()
        while True:
            now = asyncio.get_event_loop().time()
            dt = now - last
            last = now
            sim.tick(min(dt, 0.25) * sim.speed)
            acc += dt
            if acc >= 1.0 / BROADCAST_HZ and clients:
                acc = 0.0
                msg = json.dumps({"type": "state", **sim.snapshot()})
                for ws in list(clients):
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        clients.discard(ws)
            await asyncio.sleep(STEP)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        clients.add(websocket)
        await websocket.send_text(json.dumps({"type": "map", "build": BUILD, **sim.world.snapshot_map()}))
        try:
            while True:
                raw = await websocket.receive_text()
                cmd = json.loads(raw)
                if cmd.get("cmd") == "move":
                    if sim.chat_with:
                        sim.end_chat()
                    sim.move_player(int(cmd["x"]), int(cmd["y"]))
                elif cmd.get("cmd") == "say":
                    sim.player_say(str(cmd.get("to", "")), str(cmd.get("text", "")))
                elif cmd.get("cmd") == "end_chat":
                    sim.end_chat()
                elif cmd.get("cmd") == "wave":
                    sim.player_wave()
                elif cmd.get("cmd") == "pause":
                    sim.paused = not sim.paused
                elif cmd.get("cmd") == "speed":
                    sim.speed = float(cmd.get("value", 1.0))
                elif cmd.get("cmd") == "brain":
                    want = cmd.get("value")
                    if want == "random" and sim.brain.name != "random":
                        sim.brain = RandomBrain()
                    elif want == "jev" and sim.brain.name != "jev":
                        sim.brain = JevBrain()
                    sim.world.log(f"brain switched to {sim.brain.name}")
        except WebSocketDisconnect:
            clients.discard(websocket)

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--brain", choices=["jev", "random"], default="jev")
    ap.add_argument("--narrator", choices=["auto", "template", "claude"], default="auto", help="auto = claude when ANTHROPIC_API_KEY is set")
    ap.add_argument("--model", default=None, help="Jev model id (default jev-latest)")
    ap.add_argument("--narrator-model", default=None, help="Claude model for dialogue (default claude-haiku-4-5)")
    ap.add_argument("--headless", type=float, metavar="SECONDS", help="run without a server and print the log")
    args = ap.parse_args()

    sim = make_sim(args.brain, args.narrator, args.model, args.narrator_model)
    if args.headless:
        asyncio.run(run_headless(sim, args.headless))
        return 0

    import uvicorn

    print(f"village: http://127.0.0.1:{args.port}   brain={sim.brain.name} narrator={sim.narrator.name}")
    uvicorn.run(build_app(sim), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
