# Referensi dan aturan reuse

Repo berikut diaudit pada 2026-11-10. Gunakan untuk memahami **perilaku dan jebakan**.
Jangan menyalin kode, prompt, atau asetnya (lihat AGENTS.md).

| Repo | Commit yang diaudit | Lisensi | Catatan |
| --- | --- | --- | --- |
| [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) | `1379127c078f978c9908059d31f6dc6c16efbe53` | MIT | Coding agent TypeScript + Rust, fork dari Pi. TUI terminal. |
| [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` | MIT; "DeepSeek Harness" merek terdaftar (`BRAND_GUIDELINES.md`) | TypeScript, arsitektur plugin Cordis, UI web/desktop, tanpa TUI. |

## Ide yang layak diimplementasikan ulang

| Ide | Lokasi | Modul kita |
| --- | --- | --- |
| Validasi dependensi (hilang, duplikat, diri sendiri, siklus) di setiap mutasi; claim hanya jika ready | dsh `packages/team/agent-team/src/task-graph.ts`, `task-board.ts` | task graph `core` (M2) |
| CAS revisi; revisi basi ditolak | dsh `task-board.ts:128-131` | `store`, `core` |
| Log sesi dengan `seq` tervalidasi | dsh `packages/core/session/src/index.ts` | `store` |
| Baseline isolasi termasuk file untracked; merge patch vs branch; rescue branch saat gagal | omp `packages/coding-agent/src/task/worktree.ts`, `isolation-runner.ts` | `workspace`, `gate` |
| Slot provider hanya di sekitar request HTTP (perbaikan deadlock #3749) | omp `packages/coding-agent/src/task/provider-concurrency.ts` | scheduler (ADR-0010) |
| Sandbox fail-closed, default workspace-write | dsh `packages/sandbox/*`, `packages/bundle/base/cordis.patch.yml` | `workspace/exec_runner` (ADR-0008) |
| Environment proses anak yang di-scrub | dsh `packages/subprocess/subprocess/src/index.ts:66` | `security` |
| Agent eksternal lewat SDK / app-server / ACP resmi | dsh `packages/subagent/subagent-claude-code`, `subagent-codex`, `subagent-acp` | `workers` (M5) |
| Penamaan tool MCP dari config lokal; nama server remote tidak dipercaya | dsh `packages/mcp/mcp-client/README.md` | `extensions` (M6) |
| Agent Hub (roster, steer, revive, kill) | omp `docs/agent-hub.md` | TUI (M7) |

## Pola yang kita tolak

| Pola | Lokasi | Alasan |
| --- | --- | --- |
| Meniru klien resmi (string versi, fingerprint) untuk memakai kuota langganan | omp `scripts/check-spoofed-versions.ts`, `packages/ai/src/providers/claude-code-fingerprint.ts` | Melanggar terms provider (ADR-0007) |
| Auto-approve semua secara default (`yolo`) | omp `packages/coding-agent/src/tools/settings.ts:315-318` | Default tidak aman (ADR-0008) |
| Plugin pihak ketiga in-process | keduanya | Plugin mewarisi semua hak host (ADR-0009) |
| Checkout bersama untuk penulis paralel | dsh agent-team | Race pada file (ADR-0004) |
