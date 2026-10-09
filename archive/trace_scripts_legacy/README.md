# Archived trace runners

These shell runners are retained for historical experiments and compatibility
with existing logs. They are not part of the supported experiment API.

New experiments must use the single lifecycle entry point:

```bash
python3 traces/scripts/trace_runner.py run \
  --profile traces/profiles/iperf_uplink_tcpdump.json \
  --server-ip <server-ip> \
  --server-ssh sgp-server \
  --role udp_sender=<adb-serial>
```

Do not add new `run_*.sh` files here. Change or add a JSON profile and extend
the shared runner only when the workload contract requires it.
