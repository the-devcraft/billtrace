"""Record the installed runtime dependency closure; run after intentional upgrades."""

from importlib.metadata import distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

installed = {canonicalize_name(d.metadata["Name"]): d for d in distributions()}
pending = ["fastapi", "uvicorn"]
found: set[str] = set()
while pending:
    name = canonicalize_name(pending.pop())
    if name in found:
        continue
    found.add(name)
    for dependency in installed[name].requires or []:
        requirement = Requirement(dependency)
        if not requirement.marker or requirement.marker.evaluate():
            pending.append(requirement.name)

Path("requirements.lock").write_text(
    "".join(
        f"{installed[name].metadata['Name']}=={installed[name].version}\n" for name in sorted(found)
    ),
    encoding="utf-8",
)
