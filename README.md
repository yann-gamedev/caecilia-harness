# Caecilia Harness

AI orchestration harness: dekomposisi task yang sadar dependensi, sesi worker paralel di Git worktree
terisolasi, konteks bersama dengan revisi eksplisit, dan integration gate sebelum perubahan apa pun
masuk ke branch utama.

Status: **M1 berjalan**. Satu task bisa dijalankan end-to-end dengan worker palsu; worker model
sungguhan menunggu keputusan D2 (`docs/decisions-open.md`). Lihat `docs/backlog.md`.

## Mulai cepat (pengembangan)

Butuh Python 3.12+, Git 2.30+, dan [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run caecilia doctor
scripts/check.ps1        # Windows
scripts/check.sh         # Linux/macOS
```

## Pemakaian CLI

Data harness (database, artefak, worktree) disimpan di `CAECILIA_HOME` (default `~/.caecilia`),
tidak pernah di dalam repo yang dikerjakan.

```sh
# Jalankan satu task di repo PATH; gate menjalankan --check sebelum merge.
uv run caecilia run "tambahkan catatan" --repo PATH --check "python -c \"print(1)\"" --worker fake --auto-merge

uv run caecilia status            # run terakhir dan task-nya (read-only, aman saat run berjalan)
uv run caecilia runs              # daftar run terbaru
uv run caecilia resume            # lanjutkan run yang belum selesai setelah crash
```

- `--worker fake` menulis `CAECILIA_FAKE.md` berisi goal; dipakai untuk demo dan tes.
  `--worker native` butuh provider model (keputusan D2).
- Tanpa `--auto-merge`, merge ke branch target meminta konfirmasi `y/n` di terminal. Kalau stdin
  bukan terminal interaktif, merge dan approval exec otomatis ditolak.
- `--target BRANCH` memilih branch target (default: branch aktif di repo).
- Exit code: `0` DONE, `2` NEEDS_HUMAN (gate gagal terus, konflik, atau merge tidak dikonfirmasi),
  `1` gagal/dibatalkan/error.
- Ctrl+C sekali membatalkan worker dengan halus, dua kali secara paksa.
- `--plain` mematikan warna untuk log dan CI.
- Hanya satu `caecilia run`/`resume` yang boleh berjalan sekaligus; yang kedua ditolak.

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
