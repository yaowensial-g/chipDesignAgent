import re
from functools import lru_cache
from pathlib import Path

from app.storage.database import PROJECT_ROOT


SKILL_ROOT = PROJECT_ROOT / "skills"
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


class SkillCatalog:
    """Loads version-controlled role guidance without granting execution rights."""

    def __init__(self, root: Path = SKILL_ROOT) -> None:
        self.root = root.resolve()

    @lru_cache(maxsize=64)
    def load(self, name: str) -> str:
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid skill name: {name}")
        path = (self.root / f"{name}.md").resolve()
        if self.root not in path.parents:
            raise ValueError("Skill path escapes the configured root")
        if not path.is_file():
            raise KeyError(f"Skill not found: {name}")
        return path.read_text(encoding="utf-8").strip()

    def prompt(self, name: str) -> str:
        return f"\n\n以下是本角色必须遵守的项目 Skill：\n{self.load(name)}"


skill_catalog = SkillCatalog()
