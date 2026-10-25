"""Runtime settings shared by every stage of the audit pipeline.

All paths are resolved from this file's location rather than the current
working directory, so `python code/main.py` behaves the same on any machine
and from any folder (AGENTS.md section 8 asks for clone-independent paths).

Gemini is reached through Vertex AI with Application Default Credentials
(`gcloud auth application-default login`) instead of an API key, so no
secret ever has to live in the repository or in .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
SOLUTION_ROOT = CODE_DIR.parent


def _load_dotenv_if_available() -> None:
    # python-dotenv is optional: a reviewer running --no-llm should not need it
    # installed just to start the program.
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(SOLUTION_ROOT / ".env")


def _project_from_gcloud_config() -> str | None:
    """Read the active gcloud project so ADC users need no extra setting.

    Parsed from gcloud's own config file instead of running `gcloud`, which
    keeps startup fast and works where the CLI is not on PATH.
    """
    config_root = Path(os.environ.get("CLOUDSDK_CONFIG", Path.home() / ".config" / "gcloud"))
    try:
        active_name = (config_root / "active_config").read_text(encoding="utf-8").strip() or "default"
        config_text = (config_root / "configurations" / f"config_{active_name}").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in config_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "project" and value.strip():
            return value.strip()
    return None


@dataclass
class Settings:
    dataset_dir: Path = SOLUTION_ROOT / "dataset"
    output_csv_path: Path = SOLUTION_ROOT / "output.csv"
    evaluation_dir: Path = CODE_DIR / "evaluation"
    # Model answers are saved on disk so an auditor can re-run any case and
    # see the same answer and reasons. The cache is for reproducing a run,
    # not for accuracy: a wrong saved answer stays wrong.
    llm_cache_dir: Path = CODE_DIR / "llm" / "cache"
    gemini_model: str = "gemini-3.6-flash"
    google_cloud_project: str | None = None
    # Newer Gemini models are served from the global endpoint, not a region.
    google_cloud_location: str = "global"
    use_llm: bool = True
    # False (--fresh) ignores saved answers and asks Gemini again, the way a
    # real run on new requests would. New answers are still saved.
    read_cache: bool = True
    # USD per 1M tokens, Vertex AI introductory rate for gemini-3.6-flash
    # (valid through 2026-12-31; $1.50 / $7.50 from 2027-01-01). Thinking
    # tokens are billed at the output rate. Override with env variables.
    input_price_per_million: float = 0.75
    output_price_per_million: float = 3.75

    @property
    def evidence_dir(self) -> Path:
        return self.dataset_dir / "evidence"

    @classmethod
    def from_environment(cls, use_llm: bool = True, read_cache: bool = True) -> "Settings":
        _load_dotenv_if_available()
        return cls(
            use_llm=use_llm,
            read_cache=read_cache,
            gemini_model=os.environ.get("GEMINI_MODEL", cls.gemini_model),
            google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT") or _project_from_gcloud_config(),
            google_cloud_location=os.environ.get("GOOGLE_CLOUD_LOCATION", cls.google_cloud_location),
            input_price_per_million=float(os.environ.get("GEMINI_INPUT_PRICE_PER_M", cls.input_price_per_million)),
            output_price_per_million=float(os.environ.get("GEMINI_OUTPUT_PRICE_PER_M", cls.output_price_per_million)),
        )
