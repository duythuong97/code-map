"""Corpus generator and scorers.

    python tools/corpus-gen generate --out /tmp/corpus --size s|m|l [--seed 1] [--git]
    python -m code_tree_exporter extract --config /tmp/corpus/config.json
    python tools/corpus-gen score-extract --corpus /tmp/corpus [--output /tmp/corpus/out] [--sources web,db,ops] [--json]
    python tools/corpus-gen baseline --corpus /tmp/corpus                    # graph-only impact for every scenario
    python tools/corpus-gen score-impact --corpus /tmp/corpus --scenario invoice-cancel-rule --answer impact.yaml
    python tools/corpus-gen apply-scenario --corpus /tmp/corpus --scenario invoice-cancel-rule   # edit + git commit
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .generator import SIZES, generate
from .score import answer_items, format_extract_report, graph_impact, load_yaml, score_extract, score_impact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corpus-gen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--out", required=True)
    gen.add_argument("--size", choices=sorted(SIZES), default="s")
    gen.add_argument("--seed", type=int, default=1)
    gen.add_argument("--git", action="store_true", help="Make src/ a git repository with one commit (for --at and diff).")
    extract = commands.add_parser("score-extract")
    extract.add_argument("--corpus", required=True)
    extract.add_argument("--output", help="Extract output (default: <corpus>/out)")
    extract.add_argument("--sources", help="Only truth edges of these source folders, e.g. web,db,ops (no .NET)")
    extract.add_argument("--json", action="store_true")
    extract.add_argument("--fail-on-regression", action="store_true")
    baseline = commands.add_parser("baseline", help="Score the graph alone (no LLM) on every scenario.")
    baseline.add_argument("--corpus", required=True)
    baseline.add_argument("--output")
    impact = commands.add_parser("score-impact")
    impact.add_argument("--corpus", required=True)
    impact.add_argument("--scenario", required=True)
    impact.add_argument("--answer", required=True, help="impact.yaml written by the agent")
    impact.add_argument("--output")
    apply = commands.add_parser("apply-scenario")
    apply.add_argument("--corpus", required=True)
    apply.add_argument("--scenario", required=True)
    args = parser.parse_args(argv)

    if args.command == "generate":
        corpus = generate(Path(args.out).resolve(), args.size, args.seed)
        if args.git:
            source = Path(args.out).resolve() / "src"
            for command in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "corpus"]):
                subprocess.run(["git", "-C", str(source), "-c", "user.email=corpus@example.com", "-c", "user.name=corpus", *command],
                               check=True)
        print(json.dumps({"out": str(corpus.root), "domains": len(corpus.domains), "files": len(corpus.files),
                          "truth_edges": len(corpus.truth)}))
        return 0
    corpus_dir = Path(args.corpus).resolve()
    output = Path(getattr(args, "output", None) or corpus_dir / "out").resolve()
    if args.command == "score-extract":
        only = set(args.sources.split(",")) if args.sources else None
        report = score_extract(corpus_dir, output, only_sources=only)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_extract_report(report))
        return 1 if args.fail_on_regression and report["regressions"] else 0
    if args.command == "baseline":
        results = []
        for path in sorted((corpus_dir / "scenarios").glob("*.yaml")):
            scenario = load_yaml(path)
            results.append(score_impact(scenario, graph_impact(scenario, output), output))
        for result in results:
            print(f"{result['scenario']}: recall {result['recall']} ({result['found']}/{result['total']}) "
                  + " ".join(f"{key}={value['recall']}" for key, value in sorted(result["by_expected"].items())))
            for miss in result["missed"][:10]:
                print(f"  - missed {miss}")
        return 0
    scenario_path = corpus_dir / "scenarios" / f"{args.scenario}.yaml"
    scenario = load_yaml(scenario_path)
    if args.command == "score-impact":
        result = score_impact(scenario, answer_items(load_yaml(Path(args.answer))), output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "apply-scenario":
        for change in scenario.get("patch") or []:
            path = corpus_dir / change["file"]
            text = path.read_text(encoding="utf-8")
            if change["find"] not in text:
                print(f"error: {change['file']}: text to replace not found", file=sys.stderr)
                return 2
            path.write_text(text.replace(change["find"], change["replace"], 1), encoding="utf-8")
        source = corpus_dir / "src"
        if (source / ".git").exists():
            subprocess.run(["git", "-C", str(source), "-c", "user.email=corpus@example.com", "-c", "user.name=corpus",
                            "commit", "-qam", f"scenario {args.scenario}"], check=True)
        print(json.dumps({"applied": args.scenario, "files": [change["file"] for change in scenario.get("patch") or []]}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
