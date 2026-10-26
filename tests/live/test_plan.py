"""Live: an unattended product plan (three passes) on an empty project, checked by the plan gate and scope checks."""
import json
import re

from helpers import read_json
from helpers.live import Live

VISION = ("A command-line tool that keeps a personal reading list: add a book with its author, mark it read, and "
          "list what is still unread. Python 3, standard library only, data in a local JSON file. One user.")
UNATTENDED = ("Plan it unattended: skip brainstorm, run grill headless, and approve every pass yourself, taking the "
              "planner's recommended option for any question. Do not commit anything.")
CONTINUE = "Approve and continue to the next pass, taking the recommended option for any question. Do not commit."


def test_unattended_product_plan(fx):
    repo = fx.make_repo()
    fx.install_factory(repo)
    fx.git(repo, "add", "-A")
    fx.git(repo, "commit", "-q", "-m", "fixture: factory initialized")
    with Live(fx, repo) as live:
        live.claude(f"/software-factory:factory plan {VISION}\n\n{UNATTENDED}", timeout=2400)
        for _ in range(5):
            plan = read_json(repo / "factory" / "board.json").get("plan") or {}
            if plan.get("status") == "done":
                break
            live.claude(CONTINUE, cont=True, timeout=2400)
    board = read_json(repo / "factory" / "board.json")
    assert (board.get("plan") or {}).get("status") == "done", f"plan did not finish: {board.get('plan')}"
    for doc in ("vision.md", "product-brief.md", "architecture.md", "decisions.md", "epics.md"):
        assert (repo / "docs" / doc).exists(), doc
    for p in ("2", "3"):
        r = fx.ctl(repo, "plan-check", "--pass", p, check=False)
        assert r.returncode == 0, f"plan-check --pass {p}:\n{r.stdout}"
    arch = (repo / "docs" / "architecture.md").read_text()
    assert re.search(r"^- Test: `[^`]+`", arch, re.M) and re.search(r"^- Run: `[^`]+`", arch, re.M)
    items = [line for line in (repo / "factory" / "backlog.md").read_text().splitlines() if line.startswith("- [ ] B")]
    assert items and items[0].startswith("- [ ] B1 "), items[:3]
    log = [json.loads(line) for line in (repo / "factory" / "events.jsonl").read_text().splitlines() if line.strip()]
    planner = [e for e in log if e.get("kind") == "scope" and e.get("role") == "planner"]
    assert len(planner) >= 3 and all(e["result"] == "ok" for e in planner), planner
