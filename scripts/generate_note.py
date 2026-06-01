"""
generate_note.py  —  daily research note generator
Pulls from: GitHub commits, Jupyter notebook diffs, Slurm job logs
Writes: notes/YYYY-MM-DD.md  (research-structured, Claude-generated)
"""

import os
import json
import base64
import hashlib
import requests
import anthropic
from datetime import datetime, timezone, timedelta

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

# ── Config ──────────────────────────────────────────────────────────────────
GITHUB_USERNAME  = os.environ["GITHUB_USERNAME"]
GITHUB_TOKEN     = os.environ["GITHUB_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
CASCADIA_HOST    = os.environ.get("CASCADIA_HOST", "")
CASCADIA_USER    = os.environ.get("CASCADIA_USER", "")
CASCADIA_SSH_KEY = os.environ.get("CASCADIA_SSH_KEY", "")   # private key contents

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
SINCE = f"{TODAY}T00:00:00Z"
UNTIL = f"{TODAY}T23:59:59Z"

GH = {"Authorization": f"Bearer {GITHUB_TOKEN}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"}

# ── GitHub commits ───────────────────────────────────────────────────────────

def get_repos():
    repos, page = [], 1
    while True:
        r = requests.get(f"https://api.github.com/users/{GITHUB_USERNAME}/repos",
                         headers=GH, params={"per_page": 100, "page": page, "type": "owner"})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos

def get_commits(repo_full_name):
    r = requests.get(f"https://api.github.com/repos/{repo_full_name}/commits",
                     headers=GH, params={"author": GITHUB_USERNAME,
                                          "since": SINCE, "until": UNTIL, "per_page": 100})
    if r.status_code == 409:
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

# ── Jupyter notebook diffs ──────────────────────────────────────────────────

def get_notebook_changes():
    """Find .ipynb files changed today and extract new/changed output cells."""
    if not HAS_NB:
        return []

    changes = []
    for repo in get_repos():
        # Get commits for today
        commits = get_commits(repo["full_name"])
        if not commits:
            continue

        for commit in commits:
            # Get files changed in this commit
            r = requests.get(f"https://api.github.com/repos/{repo['full_name']}/commits/{commit['sha']}",
                             headers=GH)
            if r.status_code != 200:
                continue
            files = r.json().get("files", [])

            for f in files:
                if not f["filename"].endswith(".ipynb"):
                    continue

                # Fetch the notebook content
                raw_r = requests.get(f"https://raw.githubusercontent.com/{repo['full_name']}/main/{f['filename']}",
                                     headers=GH)
                if raw_r.status_code != 200:
                    continue

                try:
                    nb = nbformat.reads(raw_r.text, as_version=4)
                except Exception:
                    continue

                # Extract output cells that have text/print results
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
                            outputs.append(text.strip()[:500])  # cap length

                if outputs:
                    changes.append({
                        "repo": repo["full_name"],
                        "notebook": f["filename"],
                        "commit": commit["sha"],
                        "outputs": outputs[:10],  # top 10 outputs
                    })

    return changes

# ── Slurm job logs from Cascadia ────────────────────────────────────────────

def get_slurm_jobs():
    """SSH into Cascadia and pull today's completed Slurm jobs."""
    if not HAS_SSH or not CASCADIA_HOST or not CASCADIA_SSH_KEY:
        return []

    try:
        # Write key to temp file
        key_path = "/tmp/cascadia_key"
        with open(key_path, "w") as f:
            f.write(CASCADIA_SSH_KEY)
        os.chmod(key_path, 0o600)

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = paramiko.RSAKey.from_private_key_file(key_path)
        ssh.connect(CASCADIA_HOST, username=CASCADIA_USER, pkey=key, timeout=15)

        # sacct: jobs that ended today
        cmd = f"sacct -u {CASCADIA_USER} --starttime={TODAY} --endtime={TODAY}T23:59:59 " \
              f"--format=JobID,JobName,State,Elapsed,CPUTime,ExitCode --noheader 2>/dev/null | head -50"
        _, stdout, _ = ssh.exec_command(cmd)
        sacct_output = stdout.read().decode().strip()

        # Also grab last 20 lines of any slurm-*.out files modified today
        cmd2 = f"find ~/ -name 'slurm-*.out' -newer $(date -d '{TODAY}' +%Y-%m-%d 2>/dev/null || date -j -f '%Y-%m-%d' '{TODAY}' +%Y-%m-%d) -maxdepth 5 2>/dev/null | head -5 | xargs -I{{}} tail -20 {{}}"
        _, stdout2, _ = ssh.exec_command(cmd2)
        log_tails = stdout2.read().decode().strip()

        ssh.close()
        os.remove(key_path)

        return {"sacct": sacct_output, "log_tails": log_tails}

    except Exception as e:
        print(f"  [Slurm] SSH failed: {e} — skipping")
        return {}

# ── Claude note generation ───────────────────────────────────────────────────

def generate_note(commits, notebooks, slurm):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    has_anything = commits or notebooks or slurm

    if not has_anything:
        return (f"# {TODAY}\n\n**No activity recorded today.**\n\n"
                "---\n*Auto-generated by research-notes bot.*\n"), []

    # Build context block
    sections = []

    if commits:
        lines = ["### GitHub commits"]
        for repo, data in commits.items():
            lines.append(f"\n**{repo}** — {data['description']}")
            for c in data["commits"]:
                lines.append(f"  - [{c['sha']}] {c['time'][11:16]} UTC — {c['message']}")
        sections.append("\n".join(lines))

    if notebooks:
        lines = ["### Jupyter notebook outputs (experiment results)"]
        for nb in notebooks:
            lines.append(f"\n**{nb['notebook']}** in {nb['repo']}")
            for o in nb["outputs"]:
                lines.append(f"  ```\n  {o}\n  ```")
        sections.append("\n".join(lines))

    if slurm:
        lines = ["### Slurm jobs on Cascadia HPC"]
        if slurm.get("sacct"):
            lines.append("```\n" + slurm["sacct"] + "\n```")
        if slurm.get("log_tails"):
            lines.append("Recent job log tails:\n```\n" + slurm["log_tails"][:1500] + "\n```")
        sections.append("\n".join(lines))

    context = "\n\n".join(sections)

    prompt = f"""You are a research assistant helping Akash Kharita keep a structured lab notebook.

Background on Akash:
- PhD researcher, Earth & Space Sciences, University of Washington
- Advisors: Marine Denolle (primary), Alexander Hutko, J. Renate Hartog, Stephen Malone (PNSN)
- Research: seismic event detection/classification in the Pacific Northwest using ML
- Key projects: QuakeXNet (CNN classifier: eq/px/su/no), PhaseNet generalization benchmark,
  Mount Rainier catalog (v3: 1.4M clusters), enveloc location pipeline,
  SeisBench dataset work on Cascadia HPC cluster
- Key concepts: SU (surface events), PX (explosions/summit), EQ (earthquakes), 
  PhaseNet, SeisBench, PNSN, enveloc, Cascadia HPC, SEISBENCH_CACHE_ROOT

Today is {TODAY}. Below is everything captured today. Write a structured research lab note in Markdown.

The note MUST follow this exact structure (use these exact headings):

## 🔬 What I tested / ran
Bullet list of concrete actions — experiments run, code written, jobs submitted. Be specific.

## 📊 Results & findings
The most important thing in the note. Extract any numbers, metrics, outcomes from notebook outputs or commit messages. If there are no clear results, say so explicitly.

## 💡 Hypotheses & interpretations  
What do today's results suggest? Any new ideas, model behavior explanations, or pattern observations.

## ✅ Decisions made
Any explicit choices about direction, methodology, or next steps that were locked in today.

## 🚧 Blockers & open questions
What's unclear, broken, or waiting on something.

## 📅 Plan for tomorrow
2-4 concrete next actions, in priority order.

## 🏷️ Topics
Comma-separated list of topic tags relevant to today's work, chosen from:
PhaseNet, QuakeXNet, enveloc, SeisBench, PNSN, Cascadia-HPC, Mount-Rainier-catalog,
recall-analysis, benchmark, teleseismic, waveform-processing, labelerrors, 
dataset-download, visualization, paper-writing, advisor-meeting
Add new tags if needed.

After the topics line, add one final line:
**TL;DR:** [One sentence summary of the most important thing that happened today]

---

DO NOT add a date heading (added automatically).
DO NOT add preamble before the first heading.
Be concise and technical — this is a lab notebook, not a blog post.
Extract actual numbers from the data wherever possible.

Today's activity:
{context}
"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    body = msg.content[0].text.strip()

    # Extract tags for metadata
    tags = []
    for line in body.split("\n"):
        if line.startswith("## 🏷️") or "Topics" in line:
            next_lines = body.split(line)
            if len(next_lines) > 1:
                tag_line = next_lines[1].strip().split("\n")[0]
                tags = [t.strip() for t in tag_line.split(",") if t.strip()]
            break

    note = f"# {TODAY}\n\n{body}\n\n---\n*Auto-generated · [GitHub](https://github.com/{GITHUB_USERNAME})*\n"
    return note, tags


def write_note(note, tags):
    os.makedirs("notes", exist_ok=True)
    path = f"notes/{TODAY}.md"
    with open(path, "w") as f:
        f.write(note)

    # Also write/update notes-index.json for the site
    index_path = "notes/index.json"
    index = {}
    if os.path.exists(index_path):
        with open(index_path) as f:
            index = json.load(f)

    # Extract TL;DR
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

    print("  Calling Claude...")
    note, tags = generate_note(commits, notebooks, slurm)
    write_note(note, tags)
    print("Done.")


if __name__ == "__main__":
    main()
