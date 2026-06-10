<p align="center">
  <img src="https://seedba.se/seedbase-logo-256.png" alt="Seedbase" width="120" />
</p>

# Seedbase

Generate realistic, relationship-preserving, privacy-safe test data for your databases — and pull it straight into your local or CI database.

Seedbase lives on [seedba.se](https://seedba.se): you model (or import) a schema there, generate datasets, and use this package to pull them into Postgres, MySQL, SQLite and more. Schema-aware, foreign-key-correct, reproducible by seed.

## Install

```bash
pip install seedbase
```

Zero dependencies — pure Python (3.10+).

## CLI quickstart

```bash
seedbase login                 # opens the browser, stores a token
seedbase init                  # creates .seedbase.json (project + target DB)
seedbase generate --seed 42    # trigger a generation on the platform
seedbase pull all              # write schema + data into your target DB
```

The CLI is pull-oriented: schema and datasets live on the platform, you pull them into your database. Other commands: `projects`, `generations`, `connections`, `pull schema|data|subset`, `mask`, `db-push`, `diff`, `export config`, `import config`.

## Python SDK

```python
from seedbase import SeedbaseClient

client = SeedbaseClient(token="dr_sk_...")          # or from ~/.seedbase/config.json
gen = client.generate(project_id, seed=42, wait=True)
data = client.download(gen["id"], fmt="sql")
```

## pytest fixtures

Seedbase ships a pytest plugin, so a freshly seeded dataset is one fixture away:

```python
def test_orders(seeded_data):
    assert seeded_data  # deterministic test data pulled from the platform
```

Configure via env (`SEEDBASE_TOKEN`, `SEEDBASE_PROJECT`, `SEEDBASE_SEED`) or `pytest.ini`.

## Links

- Website: https://seedba.se
- Docs: https://seedba.se/docs
- API keys: https://seedba.se/settings?tab=api-keys

MIT licensed.
