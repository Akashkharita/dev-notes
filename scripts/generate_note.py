"""
generate_note.py  —  daily research note generator
Pulls from: GitHub commits, Jupyter notebook diffs, Slurm job logs, daily brain dump issue
Writes: notes/YYYY-MM-DD.md  (research-structured, Claude-generated)
"""

import os
import json
import requests
import anthropic
from datetime import datetime, timezone

try:
    import paramiko
    HAS_SSH = True
except ImportError:
    HAS_SSH = False

try:
    import nbformat
    HAS_NB = True
except ImportError:
    HAS_NB = False

# ── Config ───────────────────────────────────────────────────────────────────
GITHUB_USERNAME  = os.environ["GITHUB_USERNAME"]
GITHUB_TOKEN     = os.environ["GITHUB_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
CASCADIA_HOST    = os.environ.get("CASCADIA_HOST", "")
CASCADIA_USER    = os.environ.get("CASCADIA_USER", "")
CASCADIA_SSH_KEY = os.environ.get("CASCADIA_SSH_KEY", "")
NOTES_REPO       = f"{GITHUB_USERNAME}/dev-notes"   # repo where issues are filed

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
SINCE = f"{TODAY}T00:00:00Z"
UNTIL = f"{TODAY}T23:59:59Z"

GH = {"Authorization": f"Bearer {GITHUB_TOKEN}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"}

# ── GitHub commits ────────────────────────────────────────────────────────────

def get_repos():
    repos, page = [], 1
    while True:
        r = requests.get("https://api.github.com/user/repos", headers=GH,
                         params={"per_page": 100, "page": page,
                                 "type": "all", "affiliation": "owner,collaborator,organization_member"})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1

    # Hardcoded private org repos not returned by the listing API
    EXTRA_REPOS = [
        "Denolle-Lab/phasenet-retrain",
    ]
    for repo_name in EXTRA_REPOS:
        if not any(r["full_name"] == repo_name for r in repos):
            r = requests.get(f"https://api.github.com/repos/{repo_name}", headers=GH)
            if r.status_code == 200:
                repos.append(r.json())
    return repos


def get_commits(repo_full_name):
    r = requests.get(f"https://api.github.com/repos/{repo_full_name}/commits",
                     headers=GH, params={"author": GITHUB_USERNAME,
                                          "since": SINCE, "until": UNTIL, "per_page": 100})
    if r.status_code in (409, 404):
        return []
    r.raise_for_status()
    return [{"sha": c["sha"][:7], "message": c["commit"]["message"].strip(),
             "url": c["html_url"], "time": c["commit"]["author"]["date"]}
            for c in r.json()]


def collect_commits():
    activity = {}
    for repo in get_repos():
        commits = get_commits(repo["full_name"])
        if commits:
            activity[repo["full_name"]] = {
                "url": repo["html_url"],
                "description": repo.get("description") or "",
                "commits": commits,
            }
    return activity

# ── Daily brain dump issue ────────────────────────────────────────────────────

def get_brain_dump():
    """
    Reads the most recent GitHub issue labelled 'daily-log' created today.
    Returns a dict of parsed sections, or empty dict if none found.
    """
    r = requests.get(f"https://api.github.com/repos/{NOTES_REPO}/issues",
                     headers=GH, params={"labels": "daily-log", "state": "open",
                                          "per_page": 10, "sort": "created", "direction": "desc"})
    if r.status_code != 200:
        return {}

    issues = r.json()
    # Find one created today
    today_issues = [i for i in issues if i["created_at"].startswith(TODAY)]
    if not today_issues:
        # Also check closed issues (in case user closed it)
        r2 = requests.get(f"https://api.github.com/repos/{NOTES_REPO}/issues",
                          headers=GH, params={"labels": "daily-log", "state": "closed",
                                               "per_page": 10, "sort": "created", "direction": "desc"})
        if r2.status_code == 200:
            today_issues = [i for i in r2.json() if i["created_at"].startswith(TODAY)]

    if not today_issues:
        print("  [Brain dump] No issue found for today")
        return {}

    issue = today_issues[0]
    body = issue.get("body", "")
    print(f"  [Brain dump] Found issue #{issue['number']}: {issue['title']}")

    # Parse the structured form fields from the issue body
    # GitHub renders form fields as "### Field Label\n\nValue\n\n"
    sections = {}
    current_key = None
    current_lines = []

    for line in body.split("\n"):
        if line.startswith("### "):
            if current_key:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line.replace("### ", "").strip()
            current_lines = []
        elif current_key:
            current_lines.append(line)

    if current_key:
        sections[current_key] = "\n".join(current_lines).strip()

    # Filter out empty/placeholder values
    cleaned = {}
    for k, v in sections.items():
        if v and v != "_No response_" and v.strip():
            cleaned[k] = v.strip()

    return cleaned


# ── Jupyter notebook outputs ──────────────────────────────────────────────────

def get_notebook_changes():
    if not HAS_NB:
        return []

    changes = []
    for repo in get_repos():
        commits = get_commits(repo["full_name"])
        if not commits:
            continue

        for commit in commits:
            r = requests.get(f"https://api.github.com/repos/{repo['full_name']}/commits/{commit['sha']}",
                             headers=GH)
            if r.status_code != 200:
                continue
            files = r.json().get("files", [])

            for f in files:
                if not f["filename"].endswith(".ipynb"):
                    continue

                raw_r = requests.get(
                    f"https://raw.githubusercontent.com/{repo['full_name']}/main/{f['filename']}",
                    headers=GH)
                if raw_r.status_code != 200:
                    continue

                try:
                    nb = nbformat.reads(raw_r.text, as_version=4)
                except Exception:
                    continue

                outputs = []
                for cell in nb.cells:
                    if cell.cell_type != "code":
                        continue
                    for output in cell.get("outputs", []):
                        text = ""
                        if output.get("output_type") == "stream":
                            text = "".join(output.get("text", []))
                        elif output.get("output_type") in ("display_data", "execute_result"):
                            text = "".join(output.get("data", {}).get("text/plain", []))
                        if text.strip():
                            outputs.append(text.strip()[:500])

                if outputs:
                    changes.append({
                        "repo": repo["full_name"],
                        "notebook": f["filename"],
                        "commit": commit["sha"],
                        "outputs": outputs[:10],
                    })

    return changes

# ── Slurm logs ────────────────────────────────────────────────────────────────

def get_slurm_jobs():
    if not HAS_SSH or not CASCADIA_HOST or not CASCADIA_SSH_KEY:
        return {}
    try:
        key_path = "/tmp/cascadia_key"
        with open(key_path, "w") as f:
            f.write(CASCADIA_SSH_KEY)
        os.chmod(key_path, 0o600)

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = paramiko.RSAKey.from_private_key_file(key_path)
        ssh.connect(CASCADIA_HOST, username=CASCADIA_USER, pkey=key, timeout=15)

        cmd = f"sacct -u {CASCADIA_USER} --starttime={TODAY} --endtime={TODAY}T23:59:59 " \
              f"--format=JobID,JobName,State,Elapsed,CPUTime,ExitCode --noheader 2>/dev/null | head -50"
        _, stdout, _ = ssh.exec_command(cmd)
        sacct_output = stdout.read().decode().strip()

        ssh.close()
        os.remove(key_path)
        return {"sacct": sacct_output}
    except Exception as e:
        print(f"  [Slurm] SSH failed: {e}")
        return {}

# ── Claude note generation ────────────────────────────────────────────────────

def generate_note(commits, notebooks, slurm, brain_dump):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    has_anything = commits or notebooks or slurm or brain_dump

    if not has_anything:
        return (f"# {TODAY}\n\n**No activity recorded today.**\n\n"
                "---\n*Auto-generated by research-notes bot.*\n"), []

    # ── Build context ─────────────────────────────────────────────────────────
    sections = []

    if brain_dump:
        lines = ["### 📝 Researcher's own words (daily brain dump — highest priority context)"]
        for field, value in brain_dump.items():
            lines.append(f"\n**{field}**\n{value}")
        sections.append("\n".join(lines))

    if commits:
        lines = ["### GitHub commits"]
        for repo, data in commits.items():
            lines.append(f"\n**{repo}** — {data['description']}")
            for c in data["commits"]:
                lines.append(f"  - [{c['sha']}] {c['time'][11:16]} UTC — {c['message']}")
        sections.append("\n".join(lines))

    if notebooks:
        lines = ["### Jupyter notebook outputs"]
        for nb in notebooks:
            lines.append(f"\n**{nb['notebook']}** in {nb['repo']}")
            for o in nb["outputs"]:
                lines.append(f"  ```\n  {o}\n  ```")
        sections.append("\n".join(lines))

    if slurm and slurm.get("sacct"):
        sections.append(f"### Slurm jobs on Cascadia\n```\n{slurm['sacct']}\n```")

    context = "\n\n".join(sections)

    # ── Prompt ────────────────────────────────────────────────────────────────
    brain_dump_instruction = ""
    if brain_dump:
        brain_dump_instruction = """
IMPORTANT: The researcher filled in a daily brain dump form today. This is the most valuable 
input — it contains their own words about what they were thinking, what failed, key numbers, 
and decisions. Prioritize this heavily. Quote their exact words where impactful. The commits 
and notebook outputs provide supporting technical detail."""

    prompt = f"""You are a research assistant helping Akash Kharita keep a structured lab notebook.

Background:
- PhD researcher, Earth & Space Sciences, University of Washington
- Advisors: Marine Denolle (primary), Alexander Hutko, J. Renate Hartog, Stephen Malone (PNSN)
- Research: seismic event detection/classification in the Pacific Northwest using ML
- Key projects: QuakeXNet (CNN classifier: eq/px/su/no), PhaseNet generalization benchmark,
  Mount Rainier catalog (v3: 1.4M clusters), enveloc location pipeline, SeisBench datasets
- Key concepts: SU (surface events), PX (explosions), EQ (earthquakes), PhaseNet, SeisBench,
  PNSN, enveloc, Cascadia HPC, teleseismic vs local distance bins
{brain_dump_instruction}

Today is {TODAY}. Write a structured research lab note in Markdown using EXACTLY these headings:

## 🔬 What I tested / ran
Concrete actions — experiments, code written, jobs submitted. Be specific with numbers.

## 📊 Results & findings
THE most important section. Extract every number, metric, outcome. If the brain dump has 
specific numbers, make sure they appear here verbatim. Don't vague-ify concrete results.

## 💡 Hypotheses & interpretations
What do results suggest? Include the researcher's own reasoning from the brain dump if present.

## ✅ Decisions made and why
Explicit choices made today AND the reasoning behind them. This is crucial for future recall.

## ❌ What failed / surprised
Failures, dead ends, unexpected behavior. Be honest — this is a private lab notebook.

## 🚧 Blockers & open questions
What's unclear, broken, or waiting on something external.

## 📅 Tomorrow's priority
The single most important thing to do tomorrow, then 2-3 supporting tasks.

## 🏷️ Topics
Comma-separated tags from: PhaseNet, QuakeXNet, enveloc, SeisBench, PNSN, Cascadia-HPC,
Mount-Rainier-catalog, recall-analysis, benchmark, teleseismic, waveform-processing,
labelerrors, dataset-download, visualization, paper-writing, advisor-meeting
Add new tags if needed.

**TL;DR:** [One sentence — the single most important thing that happened today]

---
Rules:
- Do NOT add a date heading
- Do NOT add preamble before the first heading  
- If a section has nothing to say, write "Nothing to report." — don't omit it
- Use the researcher's own words from the brain dump where possible
- Be specific — numbers, model names, dataset names, exact error messages

Today's data:
{context}
"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    body = msg.content[0].text.strip()

    # Extract tags
    tags = []
    for line in body.split("\n"):
        if "🏷️" in line or "Topics" in line:
            idx = body.find(line)
            after = body[idx + len(line):].strip().split("\n")[0]
            tags = [t.strip() for t in after.split(",") if t.strip()]
            break

    note = f"# {TODAY}\n\n{body}\n\n---\n*Auto-generated · [GitHub](https://github.com/{GITHUB_USERNAME})*\n"
    return note, tags


def write_note(note, tags):
    os.makedirs("notes", exist_ok=True)
    path = f"notes/{TODAY}.md"
    with open(path, "w") as f:
        f.write(note)

    index_path = "notes/index.json"
    index = {}
    if os.path.exists(index_path):
        with open(index_path) as f:
            index = json.load(f)

    tldr = ""
    for line in note.split("\n"):
        if line.startswith("**TL;DR:**"):
            tldr = line.replace("**TL;DR:**", "").strip()
            break

    index[TODAY] = {"tags": tags, "tldr": tldr}
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)

    print(f"Written: {path}")
    print(f"Tags: {tags}")
    print(f"TL;DR: {tldr}")


def main():
    print(f"Generating research note for {TODAY}...")

    print("  Collecting GitHub commits...")
    commits = collect_commits()
    print(f"  Found commits in {len(commits)} repos")

    print("  Checking Jupyter notebooks...")
    notebooks = get_notebook_changes()
    print(f"  Found {len(notebooks)} notebooks with outputs")

    print("  Fetching Slurm jobs from Cascadia...")
    slurm = get_slurm_jobs()
    print(f"  Slurm data: {'yes' if slurm else 'unavailable'}")

    print("  Reading daily brain dump issue...")
    brain_dump = get_brain_dump()
    print(f"  Brain dump: {'found' if brain_dump else 'none filed today'}")

    print("  Calling Claude...")
    note, tags = generate_note(commits, notebooks, slurm, brain_dump)
    write_note(note, tags)
    print("Done.")


if __name__ == "__main__":
    main()