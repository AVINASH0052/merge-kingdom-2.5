# Merge Kingdom 2.5: Two Currencies

Answer to the Chaos Engine Labs AI Engineer assignment.

- Written answer: `Merge Kingdom 2.5 Two Currencies.docx`
- Code: a small Python model of today's save/sync and the 2.5 design, used to check the design.

## Run

Python 3, standard library only:

    python3 -m unittest discover -v

34 tests, about 7 s.

## Files

| File | What it does |
|---|---|
| `sync_today.py` | Today's client and server (Facts 2 and 3) |
| `test_sync_today.py` | Reproduces the 3 support tickets |
| `sync_v25.py` | The 2.5 design: server-owned gems, exact merge using the player_id mark |
| `test_sync_v25.py` | Same tickets with nothing lost, split, old-client cases |
| `test_sync_v25_random.py` | 3000 random histories checked against an oracle |
| `test_mutants.py` | 6 deliberately broken servers; the random test must catch each one |

Rerun one random seed:

    SEED=123 python3 -m unittest discover -p "test_sync_v25_random.py" -v
