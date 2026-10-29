# Mutation tests

Coverage says which lines the tests run; mutation testing says whether they would notice if those
lines were wrong. [mutmut](https://github.com/boxed/mutmut) makes small changes to the code (a
`<` for a `<=`, a `+` for a `-`, a string with `XX` around it, `None` for a return value...),
runs the tests against each of these *mutants*, and reports the ones that **survive**: the
change the tests did not catch. A survivor is either a gap in the tests or an equivalent change
(same behaviour), which only a reading of the diff tells apart.

[`scripts/mutation.py`](../../scripts/mutation.py) runs mutmut on four fronts, each a test suite
and the code it is held to:

| Front | Tests | Mutated code | Mutants |
| --- | --- | --- | --- |
| `unit` | `tests/unit` | `src/trade_agent` (not the Alembic migrations) | all (14,581) |
| `integration` | `tests/integration` | `src/trade_agent` (not the Alembic migrations) | all (14,581) |
| `smoke-grafana` | `tests/grafana-dashboard-smoke-tests` | `scripts/grafana_dashboards.py` | a sample (150 of 1,180) |
| `smoke-signoz` | `tests/signoz-dashboard-smoke-tests` | `scripts/signoz_dashboards.py` | a sample (150 of 3,194) |

The unit and the integration fronts mutate the same code, so their results compare: what each
suite catches on its own. The integration suite also checks that the versioned dashboards equal
the generators' output, a golden test that kills almost every mutant of the generators, so the
generators belong to the smoke fronts only: there, the question is what the smoke tests catch.

## Running

Docker must be running (Rancher Desktop). Then, from the repository root:

```bash
uv run python -m scripts.mutation run unit
uv run python -m scripts.mutation run integration
uv run python -m scripts.mutation run smoke-grafana
uv run python -m scripts.mutation run smoke-signoz
```

Each run writes `.mutation/<front>/report.md` (summary, status per module, the diffs of the
survivors, up to 5 per module) and `results.json` (every mutant with its status). Options:

| Option | Effect |
| --- | --- |
| `--sample N` | test a random sample of N mutants (`--seed`, default 1); for the smoke fronts, of the mutants the static pass leaves (default 150) |
| `--max-children N` | mutants in parallel (default: CPUs − 2, up to 8; the smoke fronts always 1) |
| `--batch N`, `--max-age MIN` | smoke fronts: mutants per stack check (25), minutes before a new stack and seed (60) |
| `--docker` / `--local` | in the runner image (the default off Linux) or here (Linux) |
| `--debug` | mutmut's own output, pytest's included (a clean run that fails, a stack that does not attach) |

Afterwards:

```bash
uv run python -m scripts.mutation report unit                    # the report again
uv run python -m scripts.mutation mutmut unit browse             # mutmut's TUI: survivors, diffs, retest
uv run python -m scripts.mutation mutmut unit show trade_agent.risk.guard.x_check__mutmut_12
uv run python -m scripts.mutation mutmut unit run "trade_agent.risk.guard*"  # retest a module
```

A front's results stay in its work copy: run it again after changing the code or the tests and
mutmut tests only the mutants of the functions that changed (and the ones never tested). A
`--sample` run and the smoke fronts name their mutants, so those run again every time.

## How it runs

**Why a container.** mutmut 3 forks a warm process per mutant (it imports pytest and the project
once) and runs only the tests that called the mutated function, which makes it fast enough for a
project of this size, but `fork()` does not exist on Windows (mutmut refuses to start there).
`scripts/mutation.py` builds [`Dockerfile`](Dockerfile) (the CI's Python and uv, the locked dev
and `mutation` dependency groups, the Docker CLI, git and Chromium) and runs the front in it. On
Linux it runs directly (`--local`), on a work copy in `.mutation/work/<front>`. mutmut is in the
`mutation` dependency group, outside the default ones: `uv sync` and the CI do not install it.

**The work copy.** The files git sees (tracked or new, not ignored: uncommitted changes count)
are copied to a work copy in the `ta-mutation` Docker volume, and mutmut builds its tree there
(`mutants/`, with every mutant of a file in the same module, chosen at run time). The volume is
mounted in the runner at the path the Docker daemon itself gives it, so the bind mounts the tests
ask for from inside the runner (the smoke stacks' Compose files, the files testcontainers maps,
`tmp_path`, which lives there too) name paths the daemon can find. The repository's git directory
is mounted read-only for the legacy regression test (`git archive` of its base commit).

**Containers.** The tests start their containers as siblings of the runner, on the host's daemon:
testcontainers for the integration tests, Compose for the smoke stacks. Everything this adds has
a `ta-mutation-` name: the runner (`ta-mutation-<front>`, one run per front at a time), the
integration front's PostgreSQL server, the smoke stacks. A new run of a front first removes what
a killed run of the same front left behind, and nothing else; the real stack is never touched.

**Integration front.** mutmut starts a pytest session per mutant, and each session started its own
PostgreSQL container. The front starts one ephemeral PostgreSQL server instead, and
`TA_TEST_POSTGRES_SERVER` ([`tests/support/database.py`](../support/database.py)) gives each
session a new database on it, migrated by the project's Alembic migrations and dropped at the end.
The lab's Freqtrade tests (`tests/integration/lab/test_lab_causality.py`, `test_lab_families.py`)
are left out: they run the agent's code in a Freqtrade container, where the mutated code cannot
be imported (it needs mutmut) and no mutant can be switched on.

**Smoke fronts.** A smoke stack takes minutes to start, and mutmut runs a pytest session per
mutant. So:

- **One stack for the run.** The front starts the suite's stack once and seeds it
  (`GRAFANA_SMOKE_ATTACH`/`SIGNOZ_SMOKE_ATTACH` name its JSON): each session attaches to it,
  regenerates the same seed in memory and provisions the mutated generator's dashboards from a
  clean slate: the stack's dashboards are deleted first, so nothing an earlier mutant left (a
  dashboard under another UID or name) reaches the next one (Grafana reads them from a folder,
  `SMOKE_GRAFANA_DASHBOARDS`, instead of the versioned JSON; SigNoz gets them through its API).
  A normal smoke run is unchanged. The stack is replaced by a new one, with
  a new seed, after `--max-age` minutes or when the UTC day changes (the dashboards read "today");
  a batch that crossed a UTC midnight, or whose clean run failed, runs again on a new stack. One
  mutant at a time: they share the stack. The SigNoz stack is renamed in the work copy
  (`ta-mutation-signoz-*`), so it never meets the stack of a normal smoke run.
- **A static pass.** Building the dashboards takes a millisecond, so every mutant of the
  generator is first built without the stack: a mutant that breaks `build()` is killed (every
  test fails at import); one that gives the same dashboards and is not used by the tests in
  another way (they call `apply`, `check`, `render`... directly) cannot be seen by any smoke
  test and is left out; the others are the candidates.
- **A sample.** A random sample of the candidates (`--sample`, 150 by default) runs against the
  smoke tests. The report gives its score with a 95% confidence interval (Wilson), and an estimate
  for the whole generator (the crashes counted as killed).
- **Kills confirmed.** A smoke test can fail for a reason of its own: under load Grafana
  sometimes stalls for half a minute (a login that times out, its Loki plugin answering 404), and
  such failures come in bursts. After the sample, every killed mutant runs again, later, in
  another session: it counts as killed only if it fails again; one that passes is reported as
  *flaky* and left out of the score.
- **Only the tests of the dashboards a mutant changes.** The plugin
  [`tests/support/mutation.py`](../support/mutation.py) builds the dashboards with and without the
  mutant and deselects the tests of the dashboards it did not change. A test says what it reads
  with the `dashboards` marker (`@pytest.mark.dashboards("ta-overview")`, `("*")` for every
  dashboard, bare for none) or with its `uid` (Grafana) / `name` (SigNoz) parameter; a test that
  says nothing always runs. **New smoke tests should say it**, or each mutant runs them.

  The tests take the panels they expect from the generator itself (`build()` at import), so a
  mutant changes the expectation together with the dashboard: a mutated title, for example,
  survives. That is what the smoke tests check today, and the report shows it.

## Run times

The first runs (2026-10-29, this machine: Docker with 12 CPUs, the real stack running beside it):

| Front | Run | Time |
| --- | --- | --- |
| `unit` | all 14,581 mutants, 6 in parallel | 36 min |
| `integration` | a sample of 1,000 (`--sample 1000`), 8 in parallel | 13 min (all: about 3 h) |
| `smoke-grafana` | 150 candidates | about 1 h |
| `smoke-signoz` | 60 candidates (`--sample 60`) | 66 min (150: about 3 h) |

A SigNoz mutant whose function the tests also use directly (`_builder`, through `events`) runs
the whole suite (about 5 minutes); one that changes a single dashboard runs its tests only (one
to two minutes); confirming the kills adds about a third. The fronts can run side by side, but
the more load, the more flaky kills (and clean runs that fail, which cost a new stack).

## Reading the report

| Status | Meaning |
| --- | --- |
| killed | a test failed: the change was caught (a timeout or a crash counts as killed too) |
| survived | every test passed with the change: read the diff (a gap in the tests, or an equivalent change) |
| no tests | no test of the suite calls the mutated function (for the smoke fronts also: the mutant changed no dashboard) |
| suspicious | the tests passed, but much slower than usual |

**Mutation score** = killed / (killed + survived), over the mutants the suite reaches; the report
also gives it counting the "no tests" ones, for the code the suite does not reach at all.

mutmut 3 mutates only the code inside functions: module-level constants (the SQL strings of
`scripts/grafana_dashboards.py`, for example) are not mutated. `# pragma: no mutate` (a line) and
`# pragma: no mutate block` leave code out; use them only for code that cannot matter (log
messages).
