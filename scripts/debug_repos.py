import os
import requests

GITHUB_USERNAME = "Akashkharita"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

GH = {"Authorization": f"Bearer {GITHUB_TOKEN}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"}

repos, page = [], 1
while True:
    r = requests.get(f"https://api.github.com/users/{GITHUB_USERNAME}/repos",
                     headers=GH, params={"per_page": 100, "page": page, "type": "all"})
    batch = r.json()
    if not batch:
        break
    repos.extend(batch)
    page += 1

print(f"Total repos found: {len(repos)}")
for repo in repos:
    print(f"  {'[private]' if repo['private'] else '[public] '} {repo['full_name']}")

# Test direct access to private org repo
r = requests.get("https://api.github.com/repos/Denolle-Lab/phasenet-retrain", headers=GH)
print(f"\nDirect access to phasenet-retrain: {r.status_code}")
if r.status_code == 200:
    print("  ✅ Can access it — hardcoding will work")
else:
    print("  ❌ Cannot access it — need org approval from Marine")
