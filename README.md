# RATT-CodeKG

RATT-CodeKG grounds JavaScript unit-test generation in the implementation of the installed npm package. It builds a CodeKG of exported callables, retrieves a source-backed contract for one exact API path, and generates one candidate in each of three scenario directions. A failing candidate can receive one repair using its contract and the execution error. Passing files are retained and exact duplicates are removed. If all three final candidates fail definitively, the first is returned with `fallback: true`.

## Requirements

Python 3.10+, npm, and Docker running Linux containers. The example uses an OpenAI-compatible model service. Use `OPENAI_API_KEY` and, for a compatible service, `OPENAI_BASE_URL`. API source and test context are sent to that service during generation and repair.

```sh
python -m pip install -r requirements.txt
npm ci --ignore-scripts
docker pull node@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e
npm install --prefix runtime --ignore-scripts --save-exact simple-statistics@7.7.6
python -m RATT build simple-statistics --runtime runtime --graphs graphs
python -m RATT generate simple-statistics simple-statistics.average --runtime runtime --graphs graphs --out generated/simple-statistics --model YOUR_MODEL_ID
```

The generation command prints the delivered paths, slot statuses, and whether selection used a fallback or is pending. To replay a delivered file, use the reported path:

```sh
python -m RATT replay generated/simple-statistics/slot-1.js --runtime runtime
```

The runner evaluates the first registered `it`, preserving the original setup, hooks, and helpers. Package reflection and test execution use offline Docker containers with read-only mounts. Rebuild the graph after changing the installed package or dependencies, and use a fresh output directory.

The public builder derives signatures, callable paths, implementation text and related definitions from the installed runtime. It supports CommonJS packages that can be loaded in the pinned Node environment. The paper's experimental setup also used repository-specific source and declaration evidence. Experiment manifests, data, scripts, runs and archived artifacts are maintained separately by the authors.

## Using RATT-CodeKG as a baseline

[Benchmark package sources](BENCHMARK_PACKAGES.md) lists the 25 packages and exact source
revisions shared with TestPilot in the paper. The quick start uses `simple-statistics@7.7.6`
and the `simple-statistics.average` API from that comparison.

For a new comparison, use the same installed package revision and exact API path for each method.
Match the model version, candidate budget, repair limit, and test runner. The published method uses
three numbered candidates, at most one repair per candidate, and the first registered `it` per
candidate. Its output can be consumed by your benchmark through the delivered paths in the JSON
response. Record each method's delivery policy. A comparison of native delivery and a comparison
using a shared selector answer different questions.

## License

MIT. See [LICENSE](LICENSE).
