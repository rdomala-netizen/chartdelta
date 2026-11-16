"""Install the bundled Claude skill (helm-chart-diff) into a skills directory."""

from __future__ import annotations

from pathlib import Path

from .core import ChartDeltaError

SKILL_NAME = "helm-chart-diff"


def skill_text() -> str:
    here = Path(__file__).resolve().parent
    candidates = [
        here / "skill" / "SKILL.md",                              # installed package
        here.parents[1] / "skills" / SKILL_NAME / "SKILL.md",     # source checkout / editable install
    ]
    for c in candidates:
        if c.is_file():
            return c.read_text(encoding="utf-8")
    raise ChartDeltaError("The bundled skill file is missing from this installation.")


def install_skill(skills_dir: Path) -> tuple[Path, str]:
    """Write SKILL.md under <skills_dir>/helm-chart-diff/. Returns (path, 'installed'|'updated'|'unchanged')."""
    text = skill_text()
    target = skills_dir.expanduser() / SKILL_NAME / "SKILL.md"
    if target.is_file():
        if target.read_text(encoding="utf-8") == text:
            return target, "unchanged"
        state = "updated"
    else:
        state = "installed"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ChartDeltaError(f"Cannot write {target}: {exc}") from None
    return target, state
