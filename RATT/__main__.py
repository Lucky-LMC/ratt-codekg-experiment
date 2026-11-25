"""Build a CodeKG or generate tests for one exact npm API path."""
import argparse
import json
import os
from pathlib import Path

from codekgs.build import build_codekg

from .pipeline import generate_tests
from .runtime import run_first

ROOT = Path(__file__).resolve().parent.parent


class ChatClient:
    def __init__(self, model):
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(timeout=120, max_retries=0)

    def call_with_system_prompt(self, system, prompt, *, log_io=False):
        response = self.client.chat.completions.create(
            model=self.model, temperature=0.7, max_tokens=2048,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    def call_simple(self, prompt, *, log_io=False):
        response = self.client.chat.completions.create(
            model=self.model, temperature=0.7, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build a CodeKG for one locally installed package")
    build.add_argument("package")
    build.add_argument("--graphs", required=True, type=Path)
    build.add_argument("--source-revision")
    generate = commands.add_parser("generate", help="generate and deliver tests for one API")
    generate.add_argument("package")
    generate.add_argument("target")
    generate.add_argument("--graphs", required=True, type=Path)
    generate.add_argument("--out", required=True, type=Path)
    generate.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    replay = commands.add_parser("replay", help="run the first registered it in one delivered file")
    replay.add_argument("test", type=Path)
    for command in (build, generate, replay):
        command.add_argument("--runtime", required=True, type=Path)
        command.add_argument("--tools", default=ROOT, type=Path)
    args = parser.parse_args()
    if args.command == "build":
        print(build_codekg(args.package, args.runtime, args.tools, args.graphs,
                           source_revision=args.source_revision))
    elif args.command == "generate":
        if not args.model:
            generate.error("provide --model or set OPENAI_MODEL")
        result = generate_tests(args.package, args.target, args.graphs, args.runtime, args.tools,
                                ChatClient(args.model), args.out)
        print(json.dumps(result, ensure_ascii=False))
    else:
        verdict = run_first(args.test, args.runtime, args.tools)
        print(json.dumps(verdict, ensure_ascii=False))
        return int(verdict["status"] != "passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
