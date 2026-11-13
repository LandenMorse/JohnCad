# Contributing to JohnCad

Thanks for helping. JohnCad is run by teachers on a Raspberry Pi in a
classroom, so the bar for every change is: **a teacher who is not a
programmer can install it, update it and recover from a mistake.**

- What the app does and how it is laid out: [README](README.md); how it is
  deployed (two Raspberry Pis) is at the top of it
- Current implementation notes and open items: [CLAUDE.md](CLAUDE.md)
- How an install is run day to day: [docs/operations.md](docs/operations.md)

## Get it running (5 minutes, no Postgres or printer needed)

You need Python 3.13 and git.

```bash
git clone https://github.com/LandenMorse/JohnCad.git
cd JohnCad
python -m venv .venv
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

In `.env`, set `DATABASE_ENGINE=sqlite` (it already has `DEBUG=True`). Then:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Slicing and printing need OrcaSlicer, Bambuddy and a print agent, which you
don't need for most work: the tests fake all three.

To develop against Postgres, like production and CI, leave `DATABASE_ENGINE`
blank and start the database with `docker compose up -d db`.

## Before you open a pull request

Run what CI runs:

```bash
ruff check .                                  # lint
python manage.py test                         # tests
python manage.py makemigrations --check       # model changes have a migration
shellcheck -x scripts/*.sh                     # only if you touched a script
```

Tests need no setup: they use their own database, plain static files and a
temporary upload folder (`cms/test_runner.py`), so nothing is left behind.
On Windows the print agent tests are skipped (the agent is Unix-only); CI
runs them.

### What CI checks

Every pull request runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

| Job | What it proves |
|---|---|
| Lint | `ruff check .` and `shellcheck` pass |
| Tests (Postgres) | Migrations apply from scratch and the suite passes on Postgres 16, with deprecation warnings as errors |
| Production settings check | `manage.py check --deploy` is clean with `DEBUG=False` |
| Dependency vulnerabilities | `pip-audit` finds nothing in `requirements.txt` |
| Install, backup and restore drill | `scripts/setup-site.sh` installs on a clean machine (twice), the site answers, a print agent can connect to it, and a backup restores |

If a check fails, fix the cause. Don't skip or silence it; if an exception
really is right, add it with a comment that says why (see
`SILENCED_SYSTEM_CHECKS` in `cms/settings.py` for an example).

## Conventions

- **One concern per pull request**, small enough to review in one sitting
  and to revert without touching anything else.
- **Tests with the change.** A bug fix comes with a test that failed before
  it. Tests live in each app's `tests.py` (`jobs/test_agent.py` for the
  agent).
- **App boundaries.** `accounts` imports from no other app, and `jobs`
  builds on `attachments`, not the other way round (the one exception is
  described in [CLAUDE.md](CLAUDE.md)).
- **Settings come from the environment.** A new setting is read in
  `cms/settings.py` with a safe default, added to `.env.example` with a
  comment, and added to the README table. Never commit a real secret.
- **Style.** Match the code around you (single quotes, no formatter is
  enforced). `ruff.toml` only enables rules that catch real bugs.
- **Teacher-facing text** (pages, script output, error messages) is plain
  English: say what happened and what to do next.
- **Docs move with the code.** If you change how something is installed,
  configured or operated, update the README or `docs/operations.md` in the
  same pull request.

## Database migrations

Installs are updated with `scripts/update.sh`, and rolled back by checking
out the previous version **without** undoing database changes. So a
migration must leave the previous release's code working:

| Change | Do this |
|---|---|
| Add a column | Make it nullable or give it a default. |
| Remove a column | Release 1: stop using it in code. Release 2: drop it. |
| Rename a column or table | Don't rename in one step. Keep the database name with `db_column`/`db_table`, or add the new one, copy the data, and remove the old one a release later. |
| Change data | A separate migration from schema changes, using `RunPython` with a reverse function (`migrations.RunPython.noop` is fine) and `apps.get_model`, never a direct model import. |

Never edit or squash a migration that is already on `master`: it has
already run on real installs.

If a change can't follow these rules, say so in the pull request and add a
note for the release notes telling teachers what to do.

## Changing the scripts in `scripts/`

Teachers run these with real data, so:

- they must be safe to run twice;
- they must stop with a clear message rather than carry on after an error
  (`set -euo pipefail`, and `die "what to do next"`);
- `restore.sh` and anything else that replaces data asks first and moves
  the old data aside rather than deleting it.

The CI install drill runs them on Ubuntu x86. It can't cover a real
Raspberry Pi, the OrcaSlicer Flatpak, Caddy/HTTPS, or the printer Pi (print
agent and Bambuddy): if you change those paths, say in the pull request how you
tested them.

## Dependencies

`requirements.txt` pins exact versions. Dependabot proposes updates weekly;
to add a package, pin it there and explain in the pull request why it is
needed. The Pi is small, so prefer the standard library and Django.

## Reporting a security problem

Please don't open a public issue. Use GitHub's **Report a vulnerability**
button on the repository's Security tab.
