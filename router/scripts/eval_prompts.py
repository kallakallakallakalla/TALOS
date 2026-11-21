"""Run one prompt version against its Langfuse dataset and print the accuracy.

    .venv/bin/python scripts/eval_prompts.py --prompt router-classification --label staging
    .venv/bin/python scripts/eval_prompts.py --prompt all --label production

Each run is stored as a Langfuse dataset run with a `correct` score per item. A version is
promoted (push_prompts.py --promote) only if it is at least as good as production.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse import Evaluation  # noqa: E402

from app import prompts  # noqa: E402
from app.classifier import DecisionClassifier  # noqa: E402
from app.config import evaluation_settings  # noqa: E402
from app.extractors import Extractors  # noqa: E402
from app.llm import StructuredLLM  # noqa: E402
from app.mailbox_client import mailbox_message_to_payload, parse_mailbox_message  # noqa: E402
from app.schemas import MailContext, Shipment  # noqa: E402

DATASETS = {
    prompts.CLASSIFY: "talos-classify",
    prompts.EXTRACT_RFQ: "talos-rfq",
    prompts.EXTRACT_TRUCKER_QUOTE: "talos-trucker-quote",
    prompts.EXTRACT_BOOKING: "talos-booking",
}


def _same(expected, actual) -> bool:
    """Field equality: numbers within 0.011, timestamps as instants, the rest exactly."""
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return actual is not None and abs(float(actual) - expected) <= 0.011
    if isinstance(expected, str) and isinstance(actual, str):
        try:  # timestamps may differ in notation ("Z" vs "+00:00") but not in the instant
            return datetime.fromisoformat(expected) == datetime.fromisoformat(actual)
        except (ValueError, TypeError):
            pass
    return expected == actual


def correct(*, output, expected_output, **_) -> Evaluation:
    """1 if every labelled field matches; the comment lists the fields that do not."""
    output = output or {}
    wrong = {
        field: {"expected": value, "got": output.get(field)}
        for field, value in expected_output.items()
        if not _same(value, output.get(field))
    }
    return Evaluation(name="correct", value=0 if wrong else 1, comment=str(wrong) if wrong else None)


def make_task(prompt_name: str, label: str):
    """The function Langfuse runs per dataset item: the app's own classifier / extractor."""
    settings = evaluation_settings()
    llm = StructuredLLM(settings)
    classifier = DecisionClassifier(settings, llm=llm)
    extractors = Extractors(settings, llm)

    async def task(*, item, **_) -> dict:
        msg = parse_mailbox_message(item.input["mail"])
        context = item.input["context"]
        if prompt_name == prompts.CLASSIFY:
            state = context["shipment_state"]
            shipment = Shipment(id=0, customer="", subject="", state=state) if state else None
            ctx = MailContext(
                sender_role=context["sender_role"], our_mail=context["our_mail"], shipment=shipment
            )
            result, _trace_id = await classifier.classify(
                mailbox_message_to_payload(msg), ctx, prompt_label=label
            )
        elif prompt_name == prompts.EXTRACT_RFQ:
            result = await extractors.extract_rfq(msg, prompt_label=label)
        elif prompt_name == prompts.EXTRACT_TRUCKER_QUOTE:
            result = await extractors.extract_trucker_quote(msg, context["our_mail"], prompt_label=label)
        else:
            result = await extractors.extract_booking(msg, prompt_label=label)
        return result.model_dump(mode="json")

    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt", default="all", choices=["all", *DATASETS])
    parser.add_argument("--label", default="staging")
    args = parser.parse_args()

    settings = evaluation_settings()
    langfuse = StructuredLLM(settings).langfuse
    for prompt_name in DATASETS if args.prompt == "all" else [args.prompt]:
        dataset = langfuse.get_dataset(DATASETS[prompt_name])
        result = dataset.run_experiment(
            name=f"{prompt_name}@{args.label}",
            task=make_task(prompt_name, args.label),
            evaluators=[correct],
            max_concurrency=8,
        )
        scores = [e for r in result.item_results for e in r.evaluations if e.name == "correct"]
        passed = sum(e.value for e in scores)
        print(f"\n{prompt_name} @ {args.label}: {passed}/{len(scores)} correct")
        for item_result in result.item_results:
            for evaluation in item_result.evaluations:
                if evaluation.name == "correct" and not evaluation.value:
                    print(f"  mail {item_result.item.input['mail']['id']}: {evaluation.comment}")
    langfuse.flush()


if __name__ == "__main__":
    main()
