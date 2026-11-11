# ADR-0006: Adapter worker mendeklarasikan kapabilitas

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Agent eksternal mengekspos hal yang berbeda-beda. Adapter Claude Code di deepseek-harness tidak mengembalikan reasoning, lalu lintas tool, usage, maupun diff ke parent.

## Keputusan

`WorkerAdapter.capabilities()` mengembalikan flag (streams_events, supports_cancel, supports_resume, reports_usage, edits_files, needs_network). Orchestrator tidak pernah bergantung pada kapabilitas yang tidak dideklarasikan. Diff selalu diambil dari worktree, bukan dari laporan worker. Native worker lebih dulu; agent eksternal hanya lewat SDK/CLI/ACP resmi.

## Alternatif yang ditolak

Membungkus TUI agent interaktif lewat PTY scraping.

## Konsekuensi

UI hanya menampilkan apa yang disediakan masing-masing adapter.
