# Caecilia Harness

AI orchestration harness: dekomposisi task yang sadar dependensi, sesi worker paralel di Git worktree
terisolasi, konteks bersama dengan revisi eksplisit, dan integration gate sebelum perubahan apa pun
masuk ke branch utama.

Status: **M1 selesai, M2 berjalan**. Run satu task atau plan multi-task paralel bisa dijalankan
end-to-end, dengan worker palsu (`--worker fake`) atau dengan model sungguhan lewat API DeepSeek
(`--worker native`), dari sesi interaktif `caecilia`, mode skrip `caecilia -p`, atau subcommand.
Lihat `docs/backlog.md`.

## Mulai cepat (pengembangan)

Butuh Python 3.12+, Git 2.30+, dan [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run caecilia doctor
scripts/check.ps1        # Windows
scripts/check.sh         # Linux/macOS
```

## Memasang `caecilia` sebagai perintah global

Supaya `caecilia` bisa diketik langsung di `cmd`, PowerShell, atau bash dari folder proyek mana pun
(seperti `claude` atau `gemini`), pasang sebagai tool uv:

```sh
# Dari folder repo harness ini. --editable: perubahan kode langsung berlaku tanpa pasang ulang.
uv tool install --editable .

# Atau dari path / URL git lain:
uv tool install C:\path\ke\caecilia-harness
uv tool install git+https://github.com/<akun>/caecilia-harness

# Kalau setelah itu `caecilia` belum dikenali, tambahkan folder tool uv ke PATH, lalu buka terminal baru:
uv tool update-shell

caecilia --version                # atau: caecilia -V
caecilia doctor                   # cek lingkungan, PATH, .caecilia.toml, dan API key
```

Memperbarui: `uv tool upgrade caecilia-harness` (tidak perlu untuk pemasangan `--editable`).
Menghapus: `uv tool uninstall caecilia-harness`.

### Konfigurasi proyek (`.caecilia.toml`)

Jalankan sekali di repo yang ingin dikerjakan; file ditulis di root repo:

```sh
cd C:\proyek\saya
caecilia init                      # ditanya: perintah check, worker, model
caecilia init --check "python -m pytest" --worker native --model <model>   # tanpa pertanyaan
```

Setelah itu cukup:

```sh
caecilia run "tambahkan endpoint health"
```

- Repo yang dikerjakan = repo Git dari folder kerja saat ini (juga dari subfolder). `--repo PATH`
  tetap bisa dipakai.
- Prioritas nilai: opsi di perintah → `.caecilia.toml` → default. Field yang tersedia: `check`,
  `worker`, `provider`, `model`, `max_parallel`, `provider_slots`, `exec_slots`, `target`,
  `allow_commands` (format di `docs/architecture.md`). Field yang tidak dikenal ditolak.
- **API key tidak pernah disimpan di file ini** (field seperti `api_key` atau `token` ditolak); pakai
  `caecilia key set <provider>`.
- `caecilia init` tidak menimpa file yang ada tanpa `--force`, dan tidak pernah meng-commit.
  `--git-add` hanya menjalankan `git add .caecilia.toml`. File yang belum di-commit tidak menghalangi
  merge ke branch yang sedang di-checkout.
- `caecilia status` dan `caecilia resume` memakai run milik repo di folder kerja; di luar repo Git,
  semua run ditampilkan.

## Sesi interaktif (`caecilia`)

Ketik `caecilia` tanpa argumen di folder proyek. Banner menampilkan versi, repo, worker/provider/model,
ada-tidaknya API key (isinya tidak pernah ditampilkan), dan run yang belum selesai.

```text
caecilia> tambahkan endpoint health dan halaman status
TASK  JUDUL      BLOCKED_BY  WRITE_SET
api   Endpoint   -           api/**
web   Halaman    api         web/**
Plan: 2 task, 1 dependensi, token planner in 1200 / out 300.
Jalankan? [y]a / [e]dit / [n]o: y
```

- Teks biasa = goal: planner membuat plan (dengan worker `fake`, plan satu task tanpa model). `e`
  membuka plan di editor (`$VISUAL`/`$EDITOR`, atau `notepad` di Windows), lalu memvalidasinya ulang;
  plan yang rusak tidak dijalankan. `n` menyimpan plan di sesi saja.
- Selama run: log event per baris, pertanyaan approval tampil di tempat yang sama. Ctrl+C sekali
  membatalkan worker dengan halus, dua kali menghentikan run, lalu kembali ke prompt. Ctrl+C saat
  menunggu input membatalkan baris; Ctrl+D (Ctrl+Z lalu Enter di Windows) atau `/exit` keluar.
- Opsi `--check`, `--worker`, `--model`, `--target`, `--max-parallel`, `--auto-merge`, `--repo`
  berlaku untuk seluruh sesi: `caecilia --worker fake --check "python -m pytest"`.
- Goal dan command disimpan di `CAECILIA_HOME/history` (jawaban pertanyaan tidak). Plan hanya ditulis
  ke file lewat `/save`.

| Command | Arti |
| --- | --- |
| `/help` | daftar command |
| `/plan <goal>` | hanya buat plan, tidak menjalankan |
| `/run [file]` | jalankan plan terakhir atau file plan |
| `/status`, `/runs`, `/graph` | sama dengan subcommand |
| `/resume [id]` | lanjutkan run |
| `/save <file>` | simpan plan terakhir |
| `/config` | opsi efektif dan asalnya (`cli` / `config` / `default`) |
| `/exit` | keluar |

## Mode skrip (`caecilia -p`)

Untuk skrip dan CI: plan → run → ringkasan, tanpa pertanyaan apa pun dan tanpa menunggu input.

```sh
caecilia -p "tambah tes untuk modul X" --auto-merge
caecilia -p --plan plan.toml --auto-merge --output-format json
caecilia -p "rapikan README" --allow-cmd "uv run pytest" --allow-cmd "git status" --quiet
```

- Exit code sama dengan `caecilia run`: `0` DONE, `2` NEEDS_HUMAN (termasuk gate lulus tetapi tidak
  di-merge), `1` gagal/dibatalkan/error.
- Merge hanya terjadi dengan `--auto-merge`.
- Perintah yang ingin dijalankan worker disetujui hanya kalau argv-nya diawali salah satu prefix
  `--allow-cmd` (boleh diulang) atau `allow_commands` di `.caecilia.toml`; sisanya ditolak, dicatat
  sebagai event, dan dicantumkan di ringkasan.
- Plan dicetak sebelum run kecuali `--quiet` (yang juga menyembunyikan log event). `--plan FILE`
  menjalankan plan yang ada tanpa planner.
- `--output-format json` mencetak satu objek JSON di akhir: `run_id`, `goal`, `status`, `exit_code`,
  `tasks` (`id`, `state`, `attempts`, `merged_commit`), `tokens`, `planner_tokens`,
  `refused_commands`. Error sebelum run menjadi `{"status": "error", "exit_code": 1, "error": ...}`.

## Pemakaian CLI

Data harness (database, artefak, worktree) disimpan di `CAECILIA_HOME` (default `~/.caecilia`),
tidak pernah di dalam repo yang dikerjakan. Contoh di bawah memakai `caecilia` global; dari repo
harness tanpa instalasi, awali dengan `uv run`.

```sh
# Satu task; gate menjalankan --check (atau `check` di .caecilia.toml) sebelum merge.
caecilia run "tambahkan catatan" --check "python -c \"print(1)\"" --worker fake --auto-merge

caecilia status            # run terakhir repo ini dan task-nya (read-only, aman saat run berjalan)
caecilia runs              # daftar run terbaru
caecilia resume            # lanjutkan run repo ini yang belum selesai setelah crash
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

Plan juga bisa dibuatkan oleh model (butuh API key, lihat "Worker native"):

```sh
uv run caecilia plan "tambah endpoint health dan halaman status" --repo PATH --model <model> --out plan.toml
```

Planner hanya **menulis file**; ia tidak pernah menjalankan run. Ia hanya melihat daftar file yang
dilacak git dan awal README (keduanya diperlakukan sebagai data, bukan instruksi), memvalidasi graf,
dan meminta model memperbaiki plan sampai 3 ronde. File yang sudah ada tidak ditimpa tanpa `--force`.
Tinjau dan edit `plan.toml`, lalu jalankan dengan `caecilia run --plan`.

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

### Mengubah graf run yang berhenti

Run yang berhenti (mis. `needs_human` atau budget habis) bisa diperbaiki tanpa membuat run baru.
Setiap perubahan menjadi revisi graf baru yang divalidasi penuh; satu masalah saja membatalkan seluruh
perubahan.

```sh
uv run caecilia graph show [--run ID]                          # urutan dependensi + revisi saat ini
uv run caecilia graph add --run ID --file task.toml [--blocked-by a,b]   # file berisi satu [[task]]
uv run caecilia graph deps --run ID TASK --blocked-by a,b      # "" = tanpa blocker; hanya task PENDING
uv run caecilia graph cancel --run ID TASK                     # bukan task yang sedang dikerjakan/di-merge
uv run caecilia graph retry --run ID TASK                      # NEEDS_HUMAN -> READY setelah Anda bereskan
uv run caecilia resume --run ID
```

- Perintah yang mengubah graf ditolak selama run sedang dieksekusi; `graph show` tetap bisa dipakai.
- Setiap perintah memakai revisi terbaru; `--expect-revision N` membuatnya gagal kalau graf sudah
  berubah sejak Anda melihatnya.

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
- Key Gemini juga bisa disimpan dan dibaca dengan cara yang sama (`caecilia key set gemini`, atau
  variabel env `GEMINI_API_KEY`), dan muncul di `caecilia key status`. Worker native dan planner
  **belum** memakainya: provider model saat ini tetap DeepSeek (keputusan D2).
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
