"""Loads and merges rule definitions from local JSON files."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.rules.rule_validator import validate_rule_file
from agent.utils.logger import get_logger

logger = get_logger(__name__)

# Path to the bundled rules directory (inside the agent package so pip includes it)
_BUNDLED_RULES_DIR = Path(__file__).resolve().parent.parent / "rules_data"


class RuleLoader:
    """Loads rules from the bundled rules/ directory or a user-supplied path.

    Rules are organised as:
        rules/
          common/common_rules.json          ← always loaded
          python/base_rules.json            ← loaded for Python projects
          python/fastapi_rules.json         ← loaded when framework == fastapi
          javascript/base_rules.json
          javascript/react_rules.json
          typescript/base_rules.json
    """

    def __init__(self, rules_dir: Optional[str] = None) -> None:
        self.rules_dir = Path(rules_dir) if rules_dir else _BUNDLED_RULES_DIR
        logger.debug("Rules directory: %s", self.rules_dir)

    def load_rules(self, language: str, framework: Optional[str]) -> List[Dict[str, Any]]:
        """Return the merged list of applicable rules for the given context.

        Load order (later files may add more rules, they do NOT override):
            1. common/common_rules.json
            2. <language>/base_rules.json
            3. <language>/<framework>_rules.json  (if framework is set)

        Args:
            language: Detected project language.
            framework: Detected framework (may be None).

        Returns:
            Flat list of enabled rule dictionaries.
        """
        lang = language.lower()
        paths_to_load: List[Path] = []

        # 1. Common rules
        common_path = self.rules_dir / "common" / "common_rules.json"
        if common_path.exists():
            paths_to_load.append(common_path)

        # 2. Language base rules
        # typescript projects also load javascript base rules
        if lang == "typescript":
            js_base = self.rules_dir / "javascript" / "base_rules.json"
            ts_base = self.rules_dir / "typescript" / "base_rules.json"
            if js_base.exists():
                paths_to_load.append(js_base)
            if ts_base.exists():
                paths_to_load.append(ts_base)
        else:
            lang_base = self.rules_dir / lang / "base_rules.json"
            if lang_base.exists():
                paths_to_load.append(lang_base)

        # 3. Framework-specific rules
        if framework:
            fw = framework.lower()
            # Map framework names to rule file names
            fw_file_map: Dict[str, str] = {
                "react_native": "react_native_rules",
                "nextjs": "nextjs_rules",
                "react": "react_rules",
                "express": "nodejs_express_rules",
                "fastapi": "fastapi_rules",
                "django": "django_rules",
                "flask": "flask_rules",
                "vue": "vue_rules",
                "angular": "angular_rules",
            }
            fw_filename = fw_file_map.get(fw, f"{fw}_rules")

            # Framework rules may live under javascript/ or python/ depending on language
            for search_lang in (lang, "javascript" if lang == "typescript" else None):
                if not search_lang:
                    continue
                fw_path = self.rules_dir / search_lang / f"{fw_filename}.json"
                if fw_path.exists():
                    paths_to_load.append(fw_path)
                    break
            else:
                # Try common frameworks directory as fallback
                fw_path = self.rules_dir / "common" / f"{fw_filename}.json"
                if fw_path.exists():
                    paths_to_load.append(fw_path)

        # Load and merge
        all_rules: List[Dict[str, Any]] = []
        loaded_ids: set = set()

        for path in paths_to_load:
            print(f"[INFO] Loading rules from : {path.name}")
            rules = self._load_file(path)
            for rule in rules:
                rule_id = rule.get("id")
                if rule_id in loaded_ids:
                    logger.debug("Skipping duplicate rule id '%s' from %s", rule_id, path.name)
                    continue
                if rule.get("enabled", True):
                    all_rules.append(rule)
                    if rule_id:
                        loaded_ids.add(rule_id)

        logger.debug(
            "Loaded %d rules for language='%s' framework='%s'",
            len(all_rules), language, framework,
        )
        return all_rules

    def _load_file(self, path: Path) -> List[Dict[str, Any]]:
        """Parse a single JSON rule file and return its validated rules list."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data: Dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in rule file %s: %s", path, exc)
            return []
        except OSError as exc:
            logger.error("Cannot read rule file %s: %s", path, exc)
            return []

        is_valid, errors = validate_rule_file(data)
        if not is_valid:
            for err in errors:
                logger.warning("Rule validation: %s", err)

        return data.get("rules", [])
