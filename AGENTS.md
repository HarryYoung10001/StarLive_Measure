# StarLive_Measure Repository Guidelines

## Layout

- `traces/` is the canonical trace collection and profile surface.
- `measurement/` contains high-precision analysis built on the shared trace package.
- `douyin_logs/` and `demo_test/` contain live-stream logcat workflows.
- `runtime/android/` and `runtime/server/` contain collection dependencies, not legacy runners.
- Runtime logs and migrated historical artifacts must not be deleted during code changes.

## Checks

```bash
bash -n runtime/android/scripts/*.sh douyin_logs/scripts/*.sh demo_test/scripts/*.sh
PYTHONPATH=src python3 -m py_compile traces/scripts/*.py measurement/scripts/*.py runtime/server/*.py
PYTHONPATH=src python3 -m unittest discover -s traces/tests -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s measurement/tests -p 'test_*.py'
```

Keep remote repository paths configurable with `--server-repo` or
`STARLIVE_SERVER_REPO`; do not reintroduce `/root/Starlink_Measure` defaults.
