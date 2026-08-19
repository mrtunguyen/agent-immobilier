"""Lists the Gemini models your key can actually call: python -m scout.models

Model ids move faster than any README. This asks the API rather than trusting a
hardcoded list, so the ids in criteria.yaml can be checked against reality before
a run fails on a 404.
"""

from __future__ import annotations

import sys

from .config import load_criteria_set, load_settings

# Anything that can't score a listing or parse an email is noise here.
_EXCLUDE_HINTS = ("image", "tts", "embedding", "aqa", "veo", "imagen")


def usable_models(client) -> list[str]:
    names = []
    for model in client.models.list():
        if "generateContent" not in (model.supported_actions or []):
            continue
        name = model.name.removeprefix("models/")
        if any(hint in name for hint in _EXCLUDE_HINTS):
            continue
        names.append(name)
    return sorted(names)


def main() -> int:
    settings = load_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY not set — check .env")
        return 2

    from google import genai

    try:
        names = usable_models(genai.Client(api_key=settings.gemini_api_key))
    except Exception as exc:
        print(f"could not list models: {exc}")
        return 1

    configured = set()
    try:
        criteria_set = load_criteria_set()
        for profile in criteria_set.all_profiles:
            configured.update({profile.parsing_model, profile.analysis_model})
    except (ValueError, OSError) as exc:
        print(f"(criteria.yaml unreadable, listing models only: {exc})\n")

    for name in names:
        print(f"  {'* ' if name in configured else '  '}{name}")
    print(f"\n{len(names)} model(s) usable for text generation; * = in criteria.yaml")

    missing = sorted(m for m in configured if m not in names)
    if missing:
        print(
            "\nWARNING: criteria.yaml names model(s) this key cannot call: "
            + ", ".join(missing)
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
