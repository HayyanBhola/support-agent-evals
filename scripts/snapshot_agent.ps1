# Refresh agent_snapshot/ from the live agent repo (read-only copy of the files evals rely on).
# Run it yourself whenever the agent changes:  .\scripts\snapshot_agent.ps1
# Claude never runs this script.
#
# agent_snapshot/ is a pure copy that only this script writes: it is deleted and re-created
# on every run. Generated files live outside it (datasets/agent_chunks.jsonl, docs in
# docs/agent_snapshot.md). After a refresh, regenerate the chunk export:
#   uv run --group snapshot python scripts/export_chunks.py

# Paths are relative to this repo; the agent repo is expected next to it.
$root = Split-Path $PSScriptRoot -Parent
$src = Join-Path (Split-Path $root -Parent) "rag-whatsapp-agent"
$dst = Join-Path $root "agent_snapshot"
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
foreach ($p in "tests\eval_cases.json","tests\run_eval.py","data","database\schema.sql","database\seed_data.sql","rag\ingest.py","rag\retriever.py","rag\chroma_store","agent\router.py","agent\verifier.py","agent\support_agent.py","agent\responder.py","agent\memory.py","api\server.py","SYSTEM_RULES.md","requirements.txt") {
  $t = Join-Path $dst $p
  New-Item -ItemType Directory -Force (Split-Path $t) | Out-Null
  Copy-Item -Recurse -Force (Join-Path $src $p) $t
}
"agent_commit=$(git -C $src rev-parse HEAD)`ncopied_at=$(Get-Date -Format o)" | Out-File -Encoding utf8 "$dst\SNAPSHOT_INFO.txt"
Get-ChildItem -Recurse -File $dst | Measure-Object | Select-Object Count
