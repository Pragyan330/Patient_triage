"""Download the protocol PDFs the grounding module cites.

    python scripts/fetch_corpus.py

The PDFs are not committed. They are third-party clinical documents, and one
of them (ESI v4) could only be found on a mirror rather than at AHRQ - which
is exactly the kind of provenance you should confirm yourself before a
clinical tool quotes page numbers out of it.

NEWS2 comes from the RCP directly and carries no copyright restriction.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

CORPUS = Path(__file__).resolve().parent.parent / "corpus"

SOURCES = [
    {
        "name": "news2_rcp_2017.pdf",
        "url": "https://www.rcp.ac.uk/media/a4ibkkbf/news2-final-report_0_0.pdf",
        "note": "NEWS2, Royal College of Physicians 2017. Official source, no "
                "copyright restriction on NEWS2.",
    },
    {
        "name": "esi_v4_handbook_2012.pdf",
        "url": "https://sgnor.ch/fileadmin/user_upload/Dokumente/Downloads/Esi_Handbook.pdf",
        "note": "ESI v4 Implementation Handbook (AHRQ, 2012). MIRROR, not AHRQ - "
                "verify before clinical use. Note v5 (ENA, 2023) supersedes v4.",
    },
]

HEADERS = {"User-Agent": "Mozilla/5.0 (patient-triage corpus fetch)"}


def fetch(source: dict) -> bool:
    target = CORPUS / source["name"]
    if target.exists():
        print(f"  have  {source['name']} ({target.stat().st_size // 1024} KB)")
        return True

    print(f"  get   {source['name']} ...", end=" ", flush=True)
    request = urllib.request.Request(source["url"], headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read()
    except Exception as exc:
        print(f"FAILED ({type(exc).__name__})")
        return False

    if not body.startswith(b"%PDF"):
        print(f"FAILED (not a PDF: {len(body)} bytes)")
        return False

    target.write_bytes(body)
    print(f"ok ({len(body) // 1024} KB)")
    return True


def main() -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    print(f"corpus -> {CORPUS}\n")
    ok = all(fetch(s) for s in SOURCES)
    print("\nprovenance:")
    for s in SOURCES:
        print(f"  {s['name']}\n    {s['note']}")
    if not ok:
        print("\nSome downloads failed. Place the PDFs in corpus/ by hand,")
        print("keeping the filenames above, then re-run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
