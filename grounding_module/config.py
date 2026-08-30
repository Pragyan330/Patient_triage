"""Runtime configuration. Everything the pipeline needs to be told, in one place."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

# handle -> (filename in the corpus dir, citation name in the output)
DOCUMENTS: dict[str, tuple[str, str]] = {
    "news2": ("news2_rcp_2017.pdf", "NEWS2 (RCP, 2017)"),
    "esi": ("esi_v4_handbook_2012.pdf", "ESI v4 Handbook (AHRQ, 2012)"),
}


@dataclass
class Config:
    corpus_dir: Path = REPO_ROOT / "corpus"

    # mistral-large-latest is tier-locked on the project account (error 1910).
    model: str = "mistral-medium-latest"
    temperature: float = 0.1

    # Reproducibility: without a fixed seed, borderline cases flip between
    # runs (an uncomplicated sore throat lands ESI 4 or 5 depending on the
    # sample). Set to None for sampling variety.
    random_seed: int | None = 7

    pages_per_lookup: int = 5
    chars_per_page: int = 2500

    # Retries for HTTP 429, which a low-tier key hits readily.
    max_retries: int = 5
    initial_backoff_seconds: float = 4.0

    documents: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DOCUMENTS))

    def api_key(self) -> str:
        key = os.getenv("MISTRAL_API_KEY") or os.getenv("mistral_api")
        if not key:
            raise RuntimeError(
                "No Mistral API key. Set MISTRAL_API_KEY (or mistral_api) in the "
                "environment or a .env file.")
        os.environ["MISTRAL_API_KEY"] = key
        return key

    def document_path(self, handle: str) -> Path:
        filename, _ = self.documents[handle]
        return self.corpus_dir / filename

    def citation_name(self, handle: str) -> str:
        return self.documents.get(handle, (None, handle))[1]


def load_dotenv_if_present(config: Config | None = None) -> None:
    """Read a .env from the repo root or its parent, if one exists.

    The key normally lives outside the repo so it cannot be committed.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (REPO_ROOT.parent / ".env", REPO_ROOT / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
