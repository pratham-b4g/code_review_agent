"""Loads and exposes agent configuration from YAML files."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "log_level": "WARNING",
    "block_on_error": True,
    "block_on_warning": False,
    "run_linting": True,
    "python_linter": "auto",     # "auto" | "ruff" | "flake8"
    "js_linter": "eslint",
    "rules_dir": None,           # None → use bundled rules/ directory
    "remote_rules_url": None,    # Optional URL for centralized rule API
    "remote_rules_token": None,  # Bearer token for rule API auth
    "exclude_paths": [
        ".git",
        "node_modules",
        "__pycache__",
        "venv",
        ".venv",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "coverage",
        ".mypy_cache",
        ".pytest_cache",
    ],
    "include_extensions": [],    # Empty → determined by detected language
    "max_file_size_kb": 500,
    "use_color": True,
    "diff_only": False,          # True → only flag violations on changed lines
    "severity_overrides": {},    # Per-project severity: {"PY003": "error", "COM002": "info"}
    "report_file_threshold": 15, # Auto-generate report file if violations exceed this
    "max_duplication_percent": 10, # Block commit if code duplication exceeds this % (0 to disable)
}

# Config file names searched in order (project root takes precedence)
_CONFIG_CANDIDATES: List[str] = [
    ".code-review-agent.yaml",
    ".code-review-agent.yml",
    "code_review_agent.yaml",
    "code_review_config.yaml",
]


class ConfigManager:
    """Manages agent configuration from YAML files with sensible defaults."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._load(config_path)

    def _load(self, explicit_path: Optional[str]) -> None:
        candidates = [explicit_path] + _CONFIG_CANDIDATES if explicit_path else _CONFIG_CANDIDATES
        for path in candidates:
            if path and Path(path).exists():
                try:
                    import yaml  # lazy import — yaml may not exist in minimal envs

                    with open(path, "r", encoding="utf-8") as fh:
                        loaded: Dict[str, Any] = yaml.safe_load(fh) or {}
                    self._config.update(loaded)
                    logger.info("Loaded config from %s", path)
                    return
                except ImportError:
                    logger.debug("PyYAML not installed; skipping %s", path)
                except Exception as exc:
                    logger.warning("Could not parse config %s: %s", path, exc)

        logger.debug("No config file found; using defaults")

    def get(self, key: str, default: Any = None) -> Any:
        """Return a config value by key."""
        return self._config.get(key, default)

    @property
    def block_on_error(self) -> bool:
        return bool(self._config.get("block_on_error", True))

    @property
    def block_on_warning(self) -> bool:
        return bool(self._config.get("block_on_warning", False))

    @property
    def exclude_paths(self) -> List[str]:
        return list(self._config.get("exclude_paths", []))

    @property
    def max_file_size_bytes(self) -> int:
        return int(self._config.get("max_file_size_kb", 500)) * 1024

    @property
    def rules_dir(self) -> Optional[str]:
        return self._config.get("rules_dir")

    @property
    def remote_rules_url(self) -> Optional[str]:
        return self._config.get("remote_rules_url") or os.getenv("REVIEW_RULES_API_URL")

    @property
    def remote_rules_token(self) -> Optional[str]:
        return self._config.get("remote_rules_token") or os.getenv("REVIEW_RULES_API_TOKEN")
