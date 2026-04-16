"""Package setup for code_review_agent."""

from pathlib import Path
from setuptools import find_packages, setup

_README = (Path(__file__).parent / "README.md").read_text(encoding="utf-8") if (
    Path(__file__).parent / "README.md"
).exists() else ""

setup(
    name="code-review-agent",
    version="2.3.1",
    description="Intelligent Python-based git pre-push code review agent",
    long_description=_README,
    long_description_content_type="text/markdown",
    author="B4G Projects",
    python_requires=">=3.9",
    packages=find_packages(exclude=["tests*"]),
    package_data={
        "agent": [
            "rules_data/common/*.json",
            "rules_data/python/*.json",
            "rules_data/javascript/*.json",
            "rules_data/typescript/*.json",
            "ai/ai_checks.yaml",
            "dashboard/static/*.html",
            "dashboard/static/*.css",
            "dashboard/static/*.js",
            "config/*.py",
            "analytics/*.py",
            "utils/email_notifier.py",
        ],
    },
    include_package_data=True,
    install_requires=[
        "pyyaml>=6.0",
        "ruff>=0.1.0",
        "groq>=0.9.0",
        "requests>=2.31.0",
        "psycopg2-binary>=2.9.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-cov",
        ]
    },
    entry_points={
        "console_scripts": [
            "cra=agent.cli:main_entry",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Quality Assurance",
    ],
)
