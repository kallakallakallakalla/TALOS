"""Turn the recorded mails and their hand labels into Langfuse datasets, one per prompt.

    .venv/bin/python scripts/build_datasets.py

Sources: tests/fixtures/mails.json (see export_mails.py) and tests/fixtures/labels.json.
Datasets: talos-classify, talos-rfq, talos-trucker-quotes (one item per pickup request, with
all trucker replies to it), talos-booking. Item ids are stable
(`<dataset>-<mail id>`), so re-running updates items instead of duplicating them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.llm import build_langfuse_client  # noqa: E402
from app.reference import Reference  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures"
# Shipment state the workflow holds when a reply of that category arrives.
_STATE_AFTER = {"rfq": "QUOTED", "customer_accept": "BOOKED", "customer_reject": "REJECTED"}


def load_items() -> dict[str, list[dict]]:
    """Dataset name -> items `{id, input, expected_output}` built from the fixtures."""
    mails = json.loads((FIXTURES / "mails.json").read_text())
    labels = json.loads((FIXTURES / "labels.json").read_text())
    reference = Reference(get_settings().start_kit_dir)
    ours = {m["id"]: m for m in mails["outbound"]}
    thread_state: dict[int, str] = {}
    datasets: dict[str, list[dict]] = {
        "talos-classify": [], "talos-rfq": [], "talos-trucker-quotes": [], "talos-booking": []
    }  # fmt: skip
    trucker_rounds: dict[str, list[tuple[dict, dict]]] = {}

    for mail in sorted(mails["inbound"], key=lambda m: m["id"]):
        label = labels.get(str(mail["id"]))
        if label is None:
            print(f"mail {mail['id']} has no label yet, skipped")
            continue
        role = reference.role_for_email(mail["from"])
        parent = ours.get(mail["in_reply_to"])
        our_mail = f"Subject: {parent['subject']}\n\n{parent['body']}" if parent else ""
        root = mail["references"][0] if mail["references"] else mail["id"]
        state = "PRICING" if role == "trucker" and parent else thread_state.get(root, "")
        context = {"sender_role": role, "shipment_state": state, "our_mail": our_mail}

        datasets["talos-classify"].append(
            {"id": mail["id"], "input": {"mail": mail, "context": context},
             "expected_output": {"category": label["category"]}}
        )  # fmt: skip
        if role == "trucker" and parent and parent["subject"].startswith("Pickup quote"):
            # Replies to the same request share its subject and arrive within the same minute.
            round_key = f"{parent['subject']}|{mail['received_at'][:15]}"
            trucker_rounds.setdefault(round_key, []).append((mail, label.get("trucker_quote", {})))
        for key, dataset in (("rfq", "talos-rfq"), ("booking", "talos-booking")):
            if key in label:
                datasets[dataset].append(
                    {"id": mail["id"], "input": {"mail": mail, "context": context},
                     "expected_output": label[key]}
                )  # fmt: skip
        if label["category"] in _STATE_AFTER and role == "customer":
            thread_state[root] = _STATE_AFTER[label["category"]]
    for replies in trucker_rounds.values():
        datasets["talos-trucker-quotes"].append(
            {"id": replies[0][0]["id"], "input": {"replies": [mail["body"] for mail, _ in replies]},
             "expected_output": {str(n): quote for n, (_, quote) in enumerate(replies, start=1)}}
        )  # fmt: skip
    return datasets


def main() -> None:
    langfuse = build_langfuse_client(get_settings())
    for name, items in load_items().items():
        langfuse.create_dataset(name=name)
        for item in items:
            langfuse.create_dataset_item(
                dataset_name=name,
                id=f"{name}-{item['id']}",
                input=item["input"],
                expected_output=item["expected_output"],
            )
        print(f"{name}: {len(items)} items")
    langfuse.flush()


if __name__ == "__main__":
    main()
