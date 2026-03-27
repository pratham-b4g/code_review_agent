"""Combines language and framework detection into a single project context object."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from agent.detector.framework_detector import FrameworkDetector
from agent.detector.language_detector import LanguageDetector
from agent.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProjectContext:
    """Holds all information about the detected project environment.

    Attributes:
        language: Primary language (e.g. 'python', 'javascript', 'typescript').
        framework: Detected framework (e.g. 'react', 'fastapi'). May be None.
        project_root: Absolute path to the repository root.
        files_to_review: Relative paths of files queued for review.
    """

    language: str
    framework: Optional[str]
    project_root: str
    files_to_review: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        fw = self.framework or "none"
        return f"ProjectContext(language={self.language}, framework={fw}, files={len(self.files_to_review)})"

    @property
    def is_typescript(self) -> bool:
        return self.language == "typescript"

    @property
    def is_javascript_family(self) -> bool:
        return self.language in ("javascript", "typescript")

    @property
    def is_python_family(self) -> bool:
        return self.language == "python"


def _detect_subproject_root(project_root: str, files_to_review: List[str]) -> str:
    """If all staged files share a common subdirectory that has its own package.json
    or requirements.txt, use that subdirectory as the detection root.

    This handles monorepos like:
        CT/
        ├── client/   (React)
        └── server/   (Express)

    If staged files are all under server/, detection runs against server/ not CT/.
    """
    if not files_to_review:
        return project_root

    root = Path(project_root).resolve()
    skip = {"node_modules", ".git", "venv", "dist", "build", "__pycache__"}

    # Find the top-level subdirectory each staged file lives in
    subdirs = set()
    for f in files_to_review:
        parts = Path(f).parts
        # parts could be absolute or relative — find the subdir under root
        try:
            rel = Path(f).resolve().relative_to(root)
            if rel.parts:
                subdirs.add(rel.parts[0])
        except ValueError:
            pass

    # If all staged files are under the same subdir, check if it has a project manifest
    if len(subdirs) == 1:
        subdir = root / list(subdirs)[0]
        if subdir.is_dir() and subdir.name not in skip:
            has_manifest = (
                (subdir / "package.json").exists()
                or (subdir / "requirements.txt").exists()
                or (subdir / "pyproject.toml").exists()
            )
            if has_manifest:
                logger.debug("Monorepo subproject detected: %s", subdir)
                return str(subdir)

    return project_root


def build_project_context(
    project_root: str,
    files_to_review: List[str],
    language_override: Optional[str] = None,
    framework_override: Optional[str] = None,
) -> ProjectContext:
    """Detect language and framework, then construct a ProjectContext.

    Args:
        project_root: Absolute or relative path to the repo root.
        files_to_review: Files that will be analyzed.
        language_override: Skip detection and use this language.
        framework_override: Skip detection and use this framework.

    Returns:
        Populated ProjectContext instance.
    """
    root = str(Path(project_root).resolve())

    # For monorepos, detect against the subproject the staged files belong to
    detection_root = _detect_subproject_root(root, files_to_review)
    if detection_root != root:
        print(f"[INFO] Subproject detected : {Path(detection_root).name}/")

    language: str
    if language_override:
        language = language_override.lower()
        logger.debug("Language override: %s", language)
    else:
        detector = LanguageDetector(project_root=detection_root)
        language = detector.detect_primary_language()
        logger.debug("Detected language: %s", language)

    framework: Optional[str]
    if framework_override:
        framework = framework_override.lower()
        logger.debug("Framework override: %s", framework)
    else:
        fw_detector = FrameworkDetector(project_root=detection_root)
        framework = fw_detector.detect()
        logger.debug("Detected framework: %s", framework)

    return ProjectContext(
        language=language,
        framework=framework,
        project_root=root,
        files_to_review=files_to_review,
    )
