# Local Dashboard

`praxist dashboard` is the browser control room for every Praxist run known to
the current host. It combines registry discovery, bounded process detection,
canonical research progress, peer health, recent log context, host resources,
and lifecycle control in one local view.

The dashboard is an operator projection. It does not become a second
orchestrator, process registry, frontier, Gems store, or resume engine.

## Open the Dashboard

With an installed CLI:

```bash
praxist dashboard
```

From a source checkout:

```bash
uv run praxist dashboard
```

The command binds `127.0.0.1:8765`, opens the local browser, and remains in the
foreground. `Ctrl-C` stops only the dashboard server. Detached Praxist runs
continue unchanged.

Use another loopback port or let the operating system select one:

```bash
praxist dashboard --port 9100
praxist dashboard --port 0 --json
```

For a monitoring-only session:

```bash
praxist dashboard --read-only
```

`--no-open` prints the local URL without opening a browser. The live parser and
generated [CLI Reference](../reference/cli.md) own the exact option contract.

Codex takeover/setup discovery is enabled by default. Disable it, or select an
explicit Codex executable, with:

```bash
praxist dashboard --no-codex-tasks
praxist dashboard --codex-bin /absolute/path/to/codex
```

The default binary order is the Codex desktop application on macOS, `codex` on
`PATH`, then Praxist's SDK-pinned Codex binary. The desktop/system preference is
intentional: an older app-server may be unable to read history written by a
newer Codex desktop release.

## What It Discovers

The fleet list uses the same merged view as `praxist status`:

- **registry** rows are managed runs whose controller identity can be checked;
- **ps-only** rows are direct Praxist controller processes without registry
  metadata;
- **stale**, **stopped**, **completed**, and **failed** rows preserve last-known
  progress for recovery or cleanup;
- **remote** rows came from a shared registry but belong to another verified
  host and cannot be controlled locally;
- **unknown** rows preserve registry facts when the local process probe is not
  permitted or cannot establish liveness.

This means one dashboard can cover multiple task projects and runs started from
different terminals or Codex sessions. It does not perform a broad filesystem
scan for unregistered run directories.

## Takeover and Setup Tasks

The separate **Takeover & setup tasks** strip is a read-only view over recent
Codex operator tasks and nearby external Praxist task projects. It uses the
[official Codex app-server protocol](https://developers.openai.com/codex/app-server)
to list recent task metadata and the newest persisted turn status. For each
unique recent working directory, it performs a bounded search for `task.yaml`:
at most three directory levels, with generated outputs, datasets, environments,
dependencies, caches, hidden directories, and the Praxist source checkout
excluded.

Cards distinguish Codex work in progress, approval waits, interruptions, task
projects ready to launch, and task projects correlated to a live canonical
Praxist run. Correlation uses the normalized `task_path` from the run registry;
it does not infer a running experiment from a Codex task alone.

A separate app-server can read persisted turn checkpoints but does not own the
desktop application's in-memory task runtime. Consequently, an active desktop
turn may temporarily show its preceding persisted checkpoint until the new turn
is stored. The card calls this a checkpoint, and a linked live Praxist registry
row always takes precedence over that operator-work status.

The available card actions are deliberately narrow:

- copy the exact resolved `codex resume <thread-id>` command to continue the
  original operator task, including the executable path when `codex` is not on
  `PATH`;
- copy the external task project path;
- open the correlated run in the canonical fleet detail view.

The dashboard does not start, interrupt, approve, or mutate a Codex task. Codex
approval requests remain in the owning Codex task. Once takeover launches a
detached run through `praxist start`, all ordinary Praxist controls appear in the
fleet and remain available through the existing authenticated lifecycle API.

## Monitoring Views

The overview shows active and known run counts, committed generations, findings,
warnings, CPU load, memory, and accelerator telemetry. Selecting a run adds:

- current generation, declared limit, phase, and contiguous committed boundary
  count;
- current generation stop-signal, results, and boundary state;
- measured best mature result and validation-only signal when materialized;
- bounded peer health, current activity, active variant, and best metric;
- canonical frontier lanes and committed Gems summaries;
- scheduler queue, capacity, activity, supply, and blocker data;
- redacted recent log context;
- compact runtime-usage and public resume-plan views.

All connected browser tabs share one sampling cache. Registry, artifact, peer,
and hardware probes run at most once per configured sampling interval, with a
minimum of one second. Browser rendering and polling do not multiply host probes
per client.

## Authority and Progress

The dashboard preserves the normal evidence hierarchy:

| Surface | Meaning |
|---|---|
| Result and finding summaries | Measured task evidence |
| `frontier/frontier_manifest.json` | Canonical lane and promotion state |
| Committed `gems/gems_state.json` | Canonical Gems state |
| Contiguous `gen_N/generation_boundary.json` files | Canonical completed-generation prefix |
| Registry, process probe, orchestrator status, peer health, logs, and scheduler | Operational telemetry |
| Codex app-server task metadata and persisted turn status | Operator-work telemetry, never proof of a live run |
| Dashboard percentages, cards, and action feed | Derived operator presentation |

If reported completed generations differ from contiguous boundary markers, the
dashboard shows an attention warning and uses committed boundaries for the
progress percentage. It does not promote a result, repair Gems, or declare a
boundary on the basis of a rendered card.

## Lifecycle Controls

Every control request is an asynchronous action. The browser remains responsive
while the action feed records queued, running, succeeded, or failed steps.

| Control | Canonical path and safety behavior |
|---|---|
| Resolve task | `praxist resolve`; read-only task and plugin validation |
| Start run | `praxist doctor`, then `praxist resolve`, then `praxist start --daemonize --json`; later steps do not run after a failed preflight |
| Stop run | `praxist stop <run-id> --grace 300 --json`; exact run ID confirmation and registry identity checks are required |
| Resume run | `praxist resume <target> --daemonize --json`; exact target confirmation and the public completed-generation recovery contract apply |
| Registry cleanup | `praxist stop --gc --json`; removes stale registry entries only and sends no signals |
| Stop all | `praxist stop --all --grace 300 --json`; requires the full host-wide confirmation phrase and is exclusive with other queued lifecycle work |

Actions are built from typed fields into argument arrays and never pass browser
text through a shell. Duplicate actions for one task or run are serialized, and
host-wide destructive actions require an idle action queue.

The dashboard intentionally does not expose arbitrary PIDs, shell commands,
manual artifact deletion, or broad `pkill` behavior. An irregular PI or Gems
boundary that the public resume plan cannot recover still requires
`praxist-control` or another explicit repair workflow with a backup and operator
approval.

## Local Security Boundary

The server accepts only IPv4 loopback hosts. It rejects non-loopback binding,
unexpected `Host` values, and cross-origin control requests.

Each dashboard process creates an ephemeral control value and injects it into
the locally served page. Mutating JSON requests must return that value in a
custom same-origin header. The value is not placed in the printed URL. The
server also emits a restrictive Content Security Policy, denies framing, enables
same-origin resource protection, serves no cross-origin headers, and caps JSON
request bodies.

Known CLI output, action output, projected JSON, and recent log lines pass
through Praxist redaction before they reach the browser. HTTP request logging is
off by default so task and run paths do not enter an extra access log. Use
`--verbose` only when local HTTP debugging requires it.

Codex conversation bodies are used neither as dashboard content nor as run
evidence. The setup projection exposes only bounded thread metadata, status,
external task identity, a copyable resume command, and canonical run
correlation.

The dashboard is not a remote operations service. Use the host's existing
secure access layer and run it locally rather than forwarding it to an
untrusted network.

## Troubleshooting

### The Fleet Is Empty but a Run Should Exist

Compare the canonical CLI view:

```bash
praxist status --json
```

If process inspection is not allowed, the dashboard shows the same probe
warning and retains readable registry rows as `unknown`. A direct process with
no registry row cannot be discovered when both the process probe and registry
metadata are unavailable.

### A Start Action Fails During Preflight

Open the action feed and inspect the failed `doctor` or `resolve` step. Fix the
task, runtime, saved login, configuration profile, or provider credential through
the normal setup workflow, then submit a new launch. The dashboard never skips a
failed preflight.

### A Takeover Task Is Missing

Wait for the background Codex sample (normally five seconds), then confirm that
the task is recent and either mentions an operational Praxist takeover flow or
has an external `task.yaml` within the bounded workspace search. If the source
chip says **unavailable**, start with the current desktop/system Codex or pass
its executable with `--codex-bin`. Use `--no-codex-tasks` when this projection
is not wanted.

### Resume Is Unavailable

A verified live or unknown controller is not offered as a resume target. Stop
the intended live run first, or inspect the run with `praxist-control` when its
boundary is irregular. Do not use forced resume to override a verified live
controller.

### The Browser Closes

Closing a tab has no effect on the dashboard server or research runs. Reopen the
printed loopback URL. `Ctrl-C` in the dashboard terminal stops the Web interface
only.
