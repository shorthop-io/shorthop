"""The public docs stay true to the code: the settings reference is generated from
app/config.py, and every link between pages resolves."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def test_configuration_reference_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "config_reference.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_every_relative_link_in_the_docs_resolves():
    broken = []
    for md in [ROOT / "README.md", *DOCS.rglob("*.md")]:
        for target in re.findall(r"\]\(([^)\s]+)\)", md.read_text()):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            path = (md.parent / target.split("#")[0]).resolve()
            if not path.exists():
                broken.append(f"{md.relative_to(ROOT)} → {target}")
    assert not broken, "\n".join(broken)
