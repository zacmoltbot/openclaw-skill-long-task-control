#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


STATE_CHOICES = ["STARTED", "CHECKPOINT", "BLOCKED", "COMPLETED"]


def parse_kv(items):
    pairs = []
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Invalid key=value pair: {item}")
        key, value = item.split("=", 1)
        pairs.append((key.strip(), value.strip()))
    return pairs


def render_block(args):
    lines = [args.state]
    lines.append(f"- task_id: {args.task_id}")

    if args.goal and args.state in {"STARTED", "COMPLETED"}:
        lines.append(f"- goal: {args.goal}")
    if args.checkpoint:
        lines.append(f"- checkpoint: {args.checkpoint}")
    if args.workflow_type:
        lines.append(f"- workflow_type: {args.workflow_type}")
    if args.stage:
        lines.append(f"- stage: {args.stage}")
    if args.blocker and args.state == "BLOCKED":
        lines.append(f"- blocker: {args.blocker}")

    facts = parse_kv(args.fact)
    outputs = args.output or []
    tried = args.tried or []
    completed = args.completed_checkpoint or []
    bg = args.background_item or []

    if args.state == "STARTED" and args.workflow_step:
        lines.append("- workflow:")
        for idx, step in enumerate(args.workflow_step, start=1):
            lines.append(f"  {idx}. {step}")

    if facts:
        lines.append("- verified facts:")
        for key, value in facts:
            lines.append(f"  - {key}={value}")

    if outputs:
        label = "- output artifacts:" if args.state == "COMPLETED" else "- outputs:"
        lines.append(label)
        for item in outputs:
            lines.append(f"  - {item}")

    if completed and args.state == "COMPLETED":
        lines.append("- completed checkpoints:")
        for item in completed:
            lines.append(f"  - {item}")

    if tried and args.state == "BLOCKED":
        lines.append("- tried:")
        for item in tried:
            lines.append(f"  - {item}")

    if args.validation and args.state == "COMPLETED":
        lines.append("- validation:")
        for item in args.validation:
            lines.append(f"  - {item}")

    if args.state == "COMPLETED":
        lines.append("- background items still running:")
        if bg:
            for item in bg:
                lines.append(f"  - {item}")
        else:
            lines.append("  - none")

    if args.need and args.state == "BLOCKED":
        lines.append("- need:")
        for item in args.need:
            lines.append(f"  - {item}")

    if args.next:
        prefix = "- handoff:" if args.state == "COMPLETED" else "- next:"
        lines.append(f"{prefix} {args.next}")

    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description="Generate standardized long-task status blocks.")
    parser.add_argument("state", choices=STATE_CHOICES)
    parser.add_argument("task_id")
    parser.add_argument("--goal")
    parser.add_argument("--checkpoint")
    parser.add_argument("--workflow-type")
    parser.add_argument("--stage")
    parser.add_argument("--workflow-step", action="append", help="Repeatable workflow step for STARTED")
    parser.add_argument("--fact", action="append", help="Repeatable key=value verified fact")
    parser.add_argument("--output", action="append", help="Repeatable artifact path/url")
    parser.add_argument("--completed-checkpoint", action="append")
    parser.add_argument("--validation", action="append")
    parser.add_argument("--background-item", action="append")
    parser.add_argument("--blocker")
    parser.add_argument("--tried", action="append")
    parser.add_argument("--need", action="append")
    parser.add_argument("--next")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit structured JSON as well")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    block = render_block(args)
    print(block)

    if args.as_json:
        payload = {
            "state": args.state,
            "task_id": args.task_id,
            "goal": args.goal,
            "checkpoint": args.checkpoint,
            "workflow_type": args.workflow_type,
            "stage": args.stage,
            "workflow": args.workflow_step or [],
            "facts": dict(parse_kv(args.fact)),
            "outputs": args.output or [],
            "completed_checkpoints": args.completed_checkpoint or [],
            "validation": args.validation or [],
            "background_items": args.background_item or [],
            "blocker": args.blocker,
            "tried": args.tried or [],
            "need": args.need or [],
            "next": args.next,
        }
        print("\nJSON")
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
