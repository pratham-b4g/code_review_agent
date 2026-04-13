"""Detects the framework used within a project (React, Django, FastAPI, etc.)."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from agent.utils.logger import get_logger

logger = get_logger(__name__)

# package.json dependency name → framework
_JS_FRAMEWORK_DEPS: Dict[str, str] = {
    "react-native": "react_native",    # must come before "react"
    "next": "nextjs",
    "react": "react",
    "vue": "vue",
    "nuxt": "nuxt",
    "@angular/core": "angular",
    "svelte": "svelte",
    "express": "express",
    "fastify": "fastify",
    "koa": "koa",
    "@hapi/hapi": "hapi",
    "@nestjs/core": "nest",
}

# Python requirement keywords → framework
_PY_FRAMEWORK_DEPS: Dict[str, str] = {
    "fastapi": "fastapi",
    "django": "django",
    "flask": "flask",
    "tornado": "tornado",
    "starlette": "starlette",
    "aiohttp": "aiohttp",
    "falcon": "falcon",
}

# Presence of these paths → framework
_PATH_FRAMEWORK: Dict[str, str] = {
    "manage.py": "django",
    "app/main.py": "fastapi",
    "app/__init__.py": "flask",
    "next.config.js": "nextjs",
    "next.config.mjs": "nextjs",
    "nuxt.config.js": "nuxt",
    "angular.json": "angular",
}


class FrameworkDetector:
    """Detects the framework used in the project."""

    def __init__(self, project_root: str = ".") -> None:
        self.root = Path(project_root)

    def detect(self) -> Optional[str]:
        """Return the detected framework name or None."""
        # Try path-based detection first (fastest, no file parsing needed)
        framework = self._detect_by_paths()
        if framework:
            return framework

        # Try package.json for JS/TS projects
        framework = self._detect_from_package_json()
        if framework:
            return framework

        # Try Python requirements
        framework = self._detect_from_requirements()
        if framework:
            return framework

        # Try pyproject.toml
        framework = self._detect_from_pyproject()
        return framework

    def _detect_by_paths(self) -> Optional[str]:
        for path_str, framework in _PATH_FRAMEWORK.items():
            if (self.root / path_str).exists():
                logger.debug("Framework detected via path '%s': %s", path_str, framework)
                return framework
        return None

    def _detect_from_package_json(self) -> Optional[str]:
        # Check root first, then one level of subdirectories (e.g. CT/server/package.json)
        candidates = [self.root / "package.json"]
        for subdir in self.root.iterdir():
            if subdir.is_dir() and subdir.name not in {"node_modules", ".git", "venv", "dist", "build"}:
                candidates.append(subdir / "package.json")

        for pkg in candidates:
            if not pkg.exists():
                continue
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Could not parse package.json: %s", exc)
                continue

            all_deps: Dict[str, str] = {
                **data.get("dependencies", {}),
                **data.get("devDependencies", {}),
            }

            # Ordered check — react-native before react, next before react
            for dep, framework in _JS_FRAMEWORK_DEPS.items():
                if dep in all_deps:
                    logger.debug("Framework detected via package.json dep '%s': %s", dep, framework)
                    return framework
        return None

    def _detect_from_requirements(self) -> Optional[str]:
        for req_file in ("requirements.txt", "requirements.in", "requirements-base.txt"):
            path = self.root / req_file
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8").lower()
            except Exception:
                continue
            for keyword, framework in _PY_FRAMEWORK_DEPS.items():
                if keyword in text:
                    logger.debug("Framework detected from %s: %s", req_file, framework)
                    return framework
        return None

    def _detect_from_pyproject(self) -> Optional[str]:
        path = self.root / "pyproject.toml"
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").lower()
            for keyword, framework in _PY_FRAMEWORK_DEPS.items():
                if keyword in text:
                    logger.debug("Framework detected from pyproject.toml: %s", framework)
                    return framework
        except Exception:
            pass
        return None

    @staticmethod
    def get_supported_frameworks() -> List[str]:
        """Return all framework identifiers that have bundled rules."""
        return [
            "react", "nextjs", "react_native", "express",
            "fastapi", "django", "flask",
            "vue", "angular",
        ]
