#!/bin/bash
# mission-control: read-only Slurm snapshot for the current user. Small output, no writes.
U="${USER:-$(whoami)}"
S=$(date -d '-2 days' +%Y-%m-%dT%H:%M:%S 2>/dev/null)
echo "### host"
hostname
date -u +%Y-%m-%dT%H:%M:%SZ
echo "### squeue"
echo "#fields JobID|Partition|Name|State|TimeUsed|TimeLimit|Nodes|CPUs|Gres|Reason|Submit|Start|WorkDir"
squeue -u "$U" -h -o '%i|%P|%j|%T|%M|%l|%D|%C|%b|%R|%V|%S|%Z' 2>&1 | head -800
echo "### sacct"
F1=JobID,JobName,Partition,State,ExitCode,ElapsedRaw,AllocCPUS,TotalCPU,ReqTRES,Start,End,NodeList,WorkDir
F2=JobID,JobName,Partition,State,ExitCode,ElapsedRaw,AllocCPUS,TotalCPU,ReqTRES,Start,End,NodeList
if OUT=$(sacct -u "$U" -S "$S" -n -P -o "$F1" 2>/dev/null); then
  echo "#fields ${F1//,/|}"
else
  OUT=$(sacct -u "$U" -S "$S" -n -P -o "$F2" 2>&1)
  echo "#fields ${F2//,/|}"
fi
printf '%s\n' "$OUT" | head -4000
echo "### sinfo"
echo "#fields Partition|Avail|Nodes(A/I/O/T)"
sinfo -s -h -o '%P|%a|%F' 2>&1 | head -60
for N in "$@"; do
  [ -z "$N" ] && continue
  echo "### node:$N"
  echo "name=$N"
  echo "resolves=$(getent hosts "$N" 2>/dev/null | awk '{print $1}' | head -1)"
  if timeout 6 bash -c "exec 3<>/dev/tcp/$N/22" 2>/dev/null; then echo "ssh_port=open"; else echo "ssh_port=closed"; fi
  echo "scontrol=$(scontrol -o show node "$N" 2>&1 | head -1 | cut -c1-1500)"
  echo "my_jobs_on_node=$(squeue -u "$U" -h -w "$N" -o '%i' 2>/dev/null | wc -l)"
done
echo "### end"
