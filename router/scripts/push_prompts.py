"""Push the local prompt files to Langfuse Prompt Management as new versions.

    .venv/bin/python scripts/push_prompts.py                    # label: staging
    .venv/bin/python scripts/push_prompts.py --label production
    .venv/bin/python scripts/push_prompts.py --promote talos-extract-rfq   # staging -> production

A prompt whose text already equals the labelled version is skipped, so re-running is safe.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.llm import build_langfuse_client  # noqa: E402
from app.prompts import ALL_PROMPTS, local_prompt  # noqa: E402


def _text(langfuse, name: str, label: str) -> str | None:
    try:
        return langfuse.get_prompt(name, label=label, cache_ttl_seconds=0).prompt
    except Exception:  # noqa: BLE001 - the prompt or the label does not exist yet
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="staging")
    parser.add_argument("--promote", metavar="NAME", help="give the staging version the production label")
    args = parser.parse_args()

    settings = get_settings()
    langfuse = build_langfuse_client(settings)

    if args.promote:
        staging = langfuse.get_prompt(args.promote, label="staging", cache_ttl_seconds=0)
        langfuse.update_prompt(
            name=args.promote, version=staging.version, new_labels=["staging", "production"]
        )
        print(f"{args.promote}: v{staging.version} is now production")
        return

    for name in ALL_PROMPTS:
        text = local_prompt(settings, name)
        if _text(langfuse, name, args.label) == text:
            print(f"{name}: unchanged")
            continue
        created = langfuse.create_prompt(name=name, prompt=text, labels=[args.label])
        print(f"{name}: pushed v{created.version} as {args.label}")


if __name__ == "__main__":
    main()
