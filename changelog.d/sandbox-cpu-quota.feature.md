New `sandbox_cpus` setting caps each sandbox container's CPU, in cores
(`MILL_SANDBOX_CPUS`, `0` = unlimited, the previous behaviour). Sandboxes
already capped memory and PIDs but nothing bounded CPU, so
`max_global_concurrency` bounded the sandbox *count* while host load stayed
unbounded — N sandboxes could take N cores. Setting a quota makes the two
proportional, which is what allows raising the concurrency cap on a small host.
