from pathlib import Path

from chartdelta.cli import main


def test_install_skill(tmp_path: Path, capsys):
    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    target = tmp_path / "helm-chart-diff" / "SKILL.md"
    assert target.read_text().startswith("---\nname: helm-chart-diff")
    assert "Skill installed:" in capsys.readouterr().out
    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    assert "Skill unchanged:" in capsys.readouterr().out
    target.write_text("old")
    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    assert "Skill updated:" in capsys.readouterr().out
    assert main(["install-skill", "--print"]) == 0
    assert "name: helm-chart-diff" in capsys.readouterr().out
