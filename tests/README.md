# Security tests

Destructive: they truncate users and repositories. Run only against a throwaway DB.

```bash
# throwaway cluster
initdb -D /tmp/olytest -U postgres -A trust && pg_ctl -D /tmp/olytest -o "-p 5499 -k /tmp" start
psql -h /tmp -p 5499 -U postgres -c "CREATE ROLE olympus LOGIN PASSWORD 'testpw'" -c "CREATE DATABASE olympusrepo OWNER olympus"
psql -h /tmp -p 5499 -U postgres -d olympusrepo -c "CREATE EXTENSION pgcrypto"
for f in sql/0*.sql; do psql -h /tmp -p 5499 -U olympus -d olympusrepo -q -f $f; done

export OLYMPUSREPO_DESTRUCTIVE_TESTS=1 OLYMPUSREPO_DB_HOST=/tmp OLYMPUSREPO_DB_PORT=5499 \
       OLYMPUSREPO_DB_PASS=testpw OLYMPUSREPO_RELAY_ENABLED=0 OLYMPUSREPO_OBJECTS_DIR=/tmp/olytest-objects
PYTHONPATH=. python tests/test_security.py
PYTHONPATH=. python tests/test_credbox.py     # needs pg_dump on PATH
```
