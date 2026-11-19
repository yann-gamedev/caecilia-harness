# Caecilia Harness

AI orchestration harness: dekomposisi task yang sadar dependensi, sesi worker paralel di Git worktree
terisolasi, konteks bersama dengan revisi eksplisit, dan integration gate sebelum perubahan apa pun
masuk ke branch utama.

Status: **M1 selesai**. Satu task bisa dijalankan end-to-end, dengan worker palsu (`--worker fake`)
atau dengan model sungguhan lewat API DeepSeek (`--worker native`). Lihat `docs/backlog.md`.

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
  `--worker native` memakai model sungguhan (lihat bagian berikut).
- Tanpa `--auto-merge`, merge ke branch target meminta konfirmasi `y/n` di terminal. Kalau stdin
  bukan terminal interaktif, merge dan approval exec otomatis ditolak.
- `--target BRANCH` memilih branch target (default: branch aktif di repo).
- Exit code: `0` DONE, `2` NEEDS_HUMAN (gate gagal terus, konflik, atau merge tidak dikonfirmasi),
  `1` gagal/dibatalkan/error.
- Ctrl+C sekali membatalkan worker dengan halus. Ctrl+C kedua menghentikan run secara paksa,
  termasuk saat menunggu jawaban approval atau konfirmasi merge; lanjutkan dengan `caecilia resume`.
- `--plain` mematikan warna untuk log dan CI.
- Hanya satu `caecilia run`/`resume` yang boleh berjalan sekaligus; yang kedua ditolak.

### Run dengan banyak task (`plan.toml`)

Untuk tujuan yang terdiri dari beberapa task dengan dependensi, tulis file plan (format lengkap di
`docs/architecture.md`, bagian "Rencana run dan penjadwalan paralel"):

```toml
version = 1
goal = "Endpoint lalu halaman"

[[task]]
id = "api"
title = "Endpoint"
instructions = "Tulis endpoint /health."
write_set = ["api/**"]

[[task]]
id = "web"
title = "Halaman"
instructions = "Tampilkan hasil /health."
write_set = ["web/**"]
blocked_by = ["api"]
```

```sh
uv run caecilia plan validate plan.toml     # periksa offline; semua masalah dicetak sekaligus
uv run caecilia run --plan plan.toml --repo PATH --check "uv run pytest" --worker fake --auto-merge
```

- `caecilia run` menerima **tepat satu** dari `GOAL` atau `--plan FILE`.
- Task independen berjalan paralel jika write set-nya terbukti terpisah. Write set yang mungkin
  beririsan tetap serial; gate dan pertanyaan pengguna selalu satu per satu.
- `--max-parallel N` (minimal 1) mengalahkan `max_parallel` di plan; default 2. Nilai disimpan untuk
  `resume`. Batas ini menghitung task LEASED/RUNNING, bukan task yang menunggu gate.
- `--provider-slots N` dan `--exec-slots N` (minimal 1, default 2) membatasi request model per provider
  dan perintah exec bersamaan. Keduanya disimpan untuk `resume`; menunggu approval tidak memakai slot exec.
- `[budget]` di plan membatasi total `max_input_tokens` dan `max_output_tokens` seluruh run. Ketika total
  melebihi batas, tidak ada lease baru; task berjalan diselesaikan, lalu run berhenti `needs_human`.
  `resume` tetap memakai total yang sudah tercatat; menaikkan budget belum tersedia.
- Diff di luar `write_set` ditolak sebelum gate dijalankan. Task masuk REWORK dengan daftar file pelanggar
  dan pola yang berlaku; setelah attempt habis, task membutuhkan bantuan pengguna.
- Setiap baris event menampilkan id task versi plan dalam `[api]` atau `[web]`; event run memakai `[-]`.
- Task baru dimulai dari head branch target saat itu, jadi ia sudah melihat hasil merge blocker-nya.
- Blocker gagal atau dibatalkan → task yang bergantung padanya dibatalkan. Blocker NEEDS_HUMAN → task
  yang bergantung tetap menunggu dan run berhenti dengan exit `2`.
- `--worker` menjadi default untuk task yang tidak menyebut `worker`; `--target` mengalahkan `target`
  di plan. `caecilia status` menampilkan kolom `BLOCKED_BY`.

### Worker native (model sungguhan)

Provider pertama adalah DeepSeek, lewat API resminya dengan **API key** (ADR-0007). Harness tidak
pernah memakai login akun konsumen. Pemakaian API tunduk pada
[DeepSeek Open Platform Terms of Service](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)
dan dikenai biaya sesuai akun Anda.

```sh
uv run caecilia key set deepseek      # input tersembunyi; disimpan di keychain OS
uv run caecilia key status            # hanya menampilkan ada/tidak, tidak pernah isi key

uv run caecilia run "<goal>" --repo PATH --check "uv run pytest" --worker native --model <model>
```

- `--model` wajib diisi dengan nama model yang tersedia di akun DeepSeek Anda; harness tidak
  menyimpan nama model bawaan.
- Untuk pengembangan, key juga bisa diberikan lewat variabel env `DEEPSEEK_API_KEY` (keychain
  didahulukan). Key tidak pernah dicetak, dicatat di event, atau diteruskan ke perintah yang
  dijalankan worker.
- Setiap perintah yang ingin dijalankan model meminta approval di terminal (`y`/`n`/`a`). Di
  Windows belum ada sandbox, jadi approval selalu ditanyakan kecuali `--unsafe-no-sandbox`.

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
