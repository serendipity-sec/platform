"""Collect official scanner metadata/licenses. Does not fetch or execute samples."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
TARGETS = [
    ("cisco-skill", "cisco-ai-defense/skill-scanner", "2.1.0", "cisco-ai-skill-scanner"),
    ("cisco-mcp", "cisco-ai-defense/mcp-scanner", "4.8.4", "cisco-ai-mcp-scanner"),
    # SkillSpector 2.11.2 is installed from the signed GitHub tag; it is not
    # published as a versioned PyPI distribution as of the collection date.
    ("skillspector", "NVIDIA/SkillSpector", "v2.11.2", None),
    ("agentshield", "affaan-m/agentshield", "v1.6.0", None),
    ("snyk", "snyk/agent-scan", "v0.6.3", None),
]

def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "serendipity-sec-research"}), timeout=45) as response:
        return response.read()

def main():
    entries = []
    for name, repo, tag, package in TARGETS:
        dest = BASE / "sources" / name
        dest.mkdir(parents=True, exist_ok=True)
        item = {"name": name, "repo": repo, "tag": tag, "files": []}
        package_label, package_url = (
            ("package-metadata.json", f"https://pypi.org/pypi/{package}/{tag.lstrip('v')}/json")
            if package else
            (("pyproject.toml", f"https://raw.githubusercontent.com/{repo}/{tag}/pyproject.toml")
             if name == "skillspector" else
             ("package.json", f"https://raw.githubusercontent.com/{repo}/{tag}/package.json"))
        )
        for label, url in [
            ("release.json", f"https://api.github.com/repos/{repo}/releases/latest"),
            ("tag-commit.json", f"https://api.github.com/repos/{repo}/commits/{tag}"),
            ("head-commit.json", f"https://api.github.com/repos/{repo}/commits?per_page=1"),
            ("LICENSE", f"https://raw.githubusercontent.com/{repo}/{tag}/LICENSE"),
            ("NOTICE", f"https://raw.githubusercontent.com/{repo}/{tag}/NOTICE"),
            ("README.md", f"https://raw.githubusercontent.com/{repo}/{tag}/README.md"),
            (package_label, package_url),
        ]:
            try:
                data = fetch(url)
                (dest / label).write_bytes(data)
                item["files"].append({"path": str((dest / label).relative_to(BASE)), "url": url, "sha256": hashlib.sha256(data).hexdigest(), "status": 200})
            except HTTPError as exc:
                if label == "NOTICE" or (name == "snyk" and label == "package.json"):
                    item["files"].append({"url": url, "status": exc.code})
                else:
                    raise
        entries.append(item)
        print(f"Collected {name} {tag}")
    (BASE / "metadata.json").write_text(json.dumps({"collected_at": datetime.now(timezone.utc).isoformat(), "scanners": entries}, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
