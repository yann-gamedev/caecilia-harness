# Referensi dan aturan reuse

Repo berikut diaudit pada 2026-11-10. Gunakan untuk memahami **perilaku dan jebakan**.
Jangan menyalin kode, prompt, atau asetnya (lihat AGENTS.md).

| Repo | Commit yang diaudit | Lisensi | Catatan |
| --- | --- | --- | --- |
| [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) | `1379127c078f978c9908059d31f6dc6c16efbe53` | MIT | Coding agent TypeScript + Rust, fork dari Pi. TUI terminal. |
| [stablyai/orca](https://github.com/stablyai/orca) | `70a1f42a33125bc935d9a55796377e2015498b16` (diaudit 2026-11-24) | MIT | IDE Electron yang mengorkestrasi agent CLI resmi. Laporan: `docs/research/orca.md`. |
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
| Dispatch preamble + laporan `worker_done` tiga kalimat + id dispatch sebagai pagar retry | orca `src/main/runtime/orchestration/workers/launch/worker-dispatch-preamble.ts` | `workers/dispatch.py` (EXT-04) |
| Koordinator tidak polling; dibangunkan saat ada surat | orca `skill-guides/orchestration.md` | `caecilia wait` (EXT-06) |
| Agent CLI resmi dijalankan headless (`claude -p`, `codex exec`, `gemini --acp`) | orca `src/shared/source-control/ai/generation/commit-message-agent-specs-primary.ts`, `src/main/gemini/gemini-acp-launch-spec.ts` | `workers/external` (EXT-02/03) |
| Agent Hub (roster, steer, revive, kill) | omp `docs/agent-hub.md` | TUI (M7) |

## Pola yang kita tolak

| Pola | Lokasi | Alasan |
| --- | --- | --- |
| Meniru klien resmi (string versi, fingerprint) untuk memakai kuota langganan | omp `scripts/check-spoofed-versions.ts`, `packages/ai/src/providers/claude-code-fingerprint.ts` | Melanggar terms provider (ADR-0007) |
| Auto-approve semua secara default (`yolo`) | omp `packages/coding-agent/src/tools/settings.ts:315-318` | Default tidak aman (ADR-0008) |
| Plugin pihak ketiga in-process | keduanya | Plugin mewarisi semua hak host (ADR-0009) |
| Checkout bersama untuk penulis paralel | dsh agent-team | Race pada file (ADR-0004) |
| Membaca kredensial OAuth agent dari keychain; banyak akun langganan dipilih berdasarkan rate limit | orca `src/main/claude-accounts/runtime-auth/keychain.ts`, `selection/claude-account-selection.ts` | Harness tidak menyentuh kredensial langganan; memutar akun berisiko melanggar terms (ADR-0018) |
