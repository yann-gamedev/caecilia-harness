# Caecilia Harness

AI orchestration harness: dekomposisi task yang sadar dependensi, sesi worker paralel di Git worktree
terisolasi, konteks bersama dengan revisi eksplisit, dan integration gate sebelum perubahan apa pun
masuk ke branch utama.

Status: **scaffold pra-M1**. Lihat `docs/backlog.md`.

## Mulai cepat (pengembangan)

Butuh Python 3.12+, Git 2.30+, dan [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run caecilia doctor
scripts/check.ps1        # Windows
scripts/check.sh         # Linux/macOS
```

## Bekerja dengan coding agent

Coding agent membaca `AGENTS.md` (Claude Code dan Gemini CLI sampai ke sana lewat `CLAUDE.md` /
`GEMINI.md`). Tugas diberikan per prompt di `docs/prompts/`, satu prompt per sesi agent:

> Baca AGENTS.md, lalu kerjakan docs/prompts/01-store-schema-migrasi.md.

## Dokumen

- `docs/architecture.md` — arsitektur v0.2
- `docs/adr/` — keputusan arsitektur
- `docs/backlog.md` — milestone, urutan task, dan dependensi
- `docs/prompts/` — prompt tugas per fitur
- `docs/decisions-open.md` — keputusan pemilik yang masih terbuka
- `docs/references.md` — repo referensi yang diaudit dan aturan reuse
