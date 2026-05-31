"""
weekly_update.py
Reads the last 7 days of notes and generates a draft advisor update email.
Writes to advisor-updates/YYYY-MM-DD.md
"""

import os
import json
import pathlib
import anthropic
from datetime import datetime, timezone, timedelta

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
TODAY = datetime.now(timezone.utc)
TODAY_STR = TODAY.strftime("%Y-%m-%d")
WEEK_START = (TODAY - timedelta(days=6)).strftime("%Y-%m-%d")


def load_week_notes():
    notes_dir = pathlib.Path("notes")
    week_notes = {}
    for md_file in sorted(notes_dir.glob("*.md")):
        date = md_file.stem
        if WEEK_START <= date <= TODAY_STR:
            week_notes[date] = md_file.read_text()
    return week_notes


def load_index():
    index_path = pathlib.Path("notes/index.json")
    if index_path.exists():
        return json.loads(index_path.read_text())
    return {}


def generate_update(week_notes, index):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    if not week_notes:
        print("No notes found for this week.")
        return None

    combined = "\n\n---\n\n".join(
        f"### {date}\n{text}" for date, text in sorted(week_notes.items())
    )

    # Collect all tags from the week
    all_tags = set()
    for date in week_notes:
        all_tags.update(index.get(date, {}).get("tags", []))

    prompt = f"""You are helping Akash Kharita write a weekly update email to his PhD advisor Marine Denolle.

Akash's research context:
- PhD, Earth & Space Sciences, University of Washington
- Primary advisor: Marine Denolle
- Research: seismic event detection/classification in Pacific Northwest using ML
- Current projects: QuakeXNet v3 classifier, PhaseNet generalization benchmark,
  Mount Rainier catalog v3 (1.4M clusters), enveloc location pipeline,
  SeisBench dataset work

Week: {WEEK_START} to {TODAY_STR}
Topics touched this week: {', '.join(sorted(all_tags)) if all_tags else 'see notes below'}

Below are Akash's daily research notes for the week. Write a concise, professional weekly update email draft to Marine.

The email should:
1. Be direct and results-focused (Marine is a busy advisor)
2. Lead with the most significant finding or progress
3. Use this structure:
   - Short opening (1 sentence on week's theme)
   - **Progress this week** — 3-5 bullet points of concrete accomplishments, with numbers where available
   - **Key finding** — 1-2 sentences on the most important result or insight
   - **Blockers / questions for you** — 1-3 items genuinely needing advisor input
   - **Plan for next week** — 3-4 bullet points
   - Professional sign-off
4. Tone: collegial but professional. Not overly formal. Not padded.
5. Keep it under 300 words.

This week's notes:
{combined}
"""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    email_draft = msg.content[0].text.strip()
    return email_draft


def write_update(draft):
    os.makedirs("advisor-updates", exist_ok=True)
    path = f"advisor-updates/{TODAY_STR}.md"
    content = f"# Advisor Update Draft — {TODAY_STR}\n\n> ⚠️ **Draft — review before sending.** Edit below, then copy into email.\n\n---\n\n{draft}\n"
    with open(path, "w") as f:
        f.write(content)
    print(f"Written: {path}")


def main():
    print(f"Generating weekly advisor update ({WEEK_START} → {TODAY_STR})...")
    notes = load_week_notes()
    print(f"  Loaded {len(notes)} daily notes")
    index = load_index()
    draft = generate_update(notes, index)
    if draft:
        write_update(draft)
        print("Done.")
    else:
        print("Nothing to write.")


if __name__ == "__main__":
    main()
