# Arsitektur v0.2

Ringkasan dari audit "Audit Arsitektur AI Orchestration Harness v0.2" (10 Nov 2026).
Status semua isi dokumen ini: **diterima untuk MVP**, sementara sampai pemilik menutup
`docs/decisions-open.md`.

Satu proses orchestrator memegang seluruh state dan kredensial. Worker menerima task, context pack
berversi, dan worktree; hasilnya diverifikasi ulang oleh integration gate sebelum menyentuh branch utama.

## Zona kepercayaan

| Zona | Isi | Dipercaya untuk | Tidak boleh |
| --- | --- | --- | --- |
| Z0 Orchestrator core | planner, scheduler, state store, policy, gate, credential broker | menulis DB, memegang API key, memutuskan merge | mengeksekusi output model secara langsung |
| Z1 Worker | native worker atau proses agent eksternal | mengubah file di worktree miliknya | menulis DB, membaca keychain, menyentuh worktree lain |
| Z2 Sandboxed exec | shell, build, tes | berjalan di dalam worktree; jaringan mati secara default | membaca `~`, SSH agent, kredensial |
| Z3 Input tak tepercaya | isi repo, output tool, MCP, web, plugin, output model | dibaca sebagai data berlabel asal-usul | dianggap instruksi atau persetujuan |

## Peta modul (`src/caecilia_harness/`)

| Package / modul | Tanggung jawab | Milestone |
| --- | --- | --- |
| `core` | model, port (Protocol), tabel state task, orchestrator, scheduler, policy | M1–M2 |
| `store` | skema SQLite, migrasi, append event, proyeksi, outbox, lease | M1 |
| `providers` | adapter model + registry kapabilitas; `fake` untuk tes | M1 |
| `workers` | implementasi `WorkerAdapter`: `native`, `fake`, lalu agent eksternal | M1, M5 |
| `workspace` | lifecycle git worktree, diff, exec runner ber-sandbox | M1 |
| `gate` | merge queue, rebase, pengecekan, contract diff | M1 (sederhana), M4 |
| `context` | context pack builder, pin, invalidasi | M3 |
| `extensions` | plugin host out-of-process, skills, MCP client | M6 |
| `security` | approval, keychain, redaksi, scrub env | M1+ |
| `interfaces` | CLI (Typer), TUI (Textual) | M1 (CLI), M7 (TUI) |
| `telemetry` | log terstruktur, event usage dan biaya | M1+ |
| `artifacts.py` | penyimpanan file content-addressed (sha256) di `CAECILIA_HOME/artifacts` | M1 |
| `bootstrap.py` | merakit store, provider, worker, workspace, gate menjadi orchestrator siap pakai | M1 |
| `paths.py` | lokasi data runtime | M0 |

Aturan dependensi: `core` tidak bergantung pada package internal mana pun; adapter bergantung pada
`core`; `bootstrap.py` boleh mengimpor semuanya; `interfaces` mengimpor `bootstrap` dan tidak diimpor
siapa pun. Ditegakkan oleh import-linter (`pyproject.toml`) dan `tests/unit/test_scaffold.py`.

Data runtime berada di luar repo yang dikerjakan: `CAECILIA_HOME` (default `~/.caecilia`) berisi
`harness.db`, artefak, log worker, dan worktree.

## State machine task

Tabel yang berlaku adalah `src/caecilia_harness/core/states.py`.

| Dari | Ke | Syarat / pemicu |
| --- | --- | --- |
| PENDING | READY | semua blocker DONE, revisi graf terbaru |
| READY | LEASED | CAS lease berhasil dan slot worker + provider tersedia |
| LEASED | RUNNING | worker mengonfirmasi mulai |
| LEASED | READY | lease dilepas sebelum mulai (worker gagal start) |
| RUNNING | AWAITING_GATE | worker sukses; harness menangkap diff dari worktree |
| AWAITING_GATE | INTEGRATING | task sampai di kepala merge queue |
| INTEGRATING | DONE | gate lolos, fast-forward berhasil |
| INTEGRATING | REWORK | gate gagal dan masih ada sisa retry |
| INTEGRATING | NEEDS_HUMAN | konflik tidak bisa di-rebase otomatis |
| REWORK | READY | rework dijadwalkan bersama laporan gate |
| RUNNING, AWAITING_GATE | STALE | dokumen yang di-pin berubah revisi |
| STALE | READY, REWORK | jalankan ulang, atau rework dengan konteks baru |
| RUNNING | RETRY_WAIT | error sementara dan task idempoten |
| RETRY_WAIT | READY | waktu backoff habis |
| RUNNING | FAILED | error permanen atau retry habis |
| LEASED, RUNNING | RECOVERING | lease kedaluwarsa (crash) |
| RECOVERING | READY, NEEDS_HUMAN | aman dijalankan ulang / efek samping tidak diketahui |
| NEEDS_HUMAN | READY | manusia sudah membereskan masalah |
| semua non-terminal | CANCELLED | dibatalkan pengguna |

Terminal: DONE, FAILED, CANCELLED. `LEASED → READY` dan `NEEDS_HUMAN → READY` adalah tambahan dari
tabel audit, supaya worker yang gagal start dan masalah yang sudah dibereskan manusia punya jalur sah.

Sesi worker: `CREATED → ACTIVE ⇄ PAUSED → TERMINATED`. Pause hanya di batas step.

## Event

Envelope: `core/models.py::Event`. `seq` bersifat global (autoincrement SQLite); `stream_seq` per
stream dengan `UNIQUE(stream, stream_seq)`, sehingga append dengan `stream_seq` yang diharapkan
menjadi CAS. `idempotency_key` unik. Payload besar disimpan sebagai artefak dan dirujuk lewat `payload_ref`.

Tipe awal: `run.created`, `run.finished`, `task.created`, `task.state_changed`, `task.lease_acquired`,
`task.lease_released`, `task.lease_expired`, `task.context_stale`, `document.updated`,
`worker.started`, `worker.result`, `gate.started`, `gate.passed`, `gate.failed`,
`approval.requested`, `approval.decided`, `usage.recorded`.

Durable vs ephemeral (ADR-0011): stream token tidak pernah masuk DB; yang masuk hanya ringkasan,
checkpoint, perubahan state, approval, dan usage.

Pengiriman: commit state terjadi tepat sekali dalam satu transaksi SQLite. Efek samping (spawn proses,
merge) lewat tabel `outbox` dan bersifat at-least-once dengan idempotency key.

## Kontrak `ModelEvent`

Semua provider memancarkan `ModelEvent` dengan bentuk `data` berikut. Stream selalu diakhiri tepat satu
event `done` atau `error`.

| `kind` | `data` |
| --- | --- |
| `text_delta` | `{"text": str}` |
| `tool_call` | `{"id": str, "name": str, "input": dict}` |
| `usage` | `{"input_tokens": int, "output_tokens": int}` |
| `done` | `{"stop_reason": "end_turn" \| "tool_use" \| "max_tokens" \| "cancelled"}` |
| `error` | `{"message": str, "retryable": bool}` |

## Storage

Skema: `src/caecilia_harness/store/schema.sql`. `context_pins` menggerakkan invalidasi: ketika
`documents.revision` naik, setiap task non-terminal yang mem-pin revisi lama mendapat `task.context_stale`.

## Aturan konkurensi

1. Satu penulis DB: orchestrator. Lease di-insert/CAS di dalam transaksi.
2. Slot (request LLM, exec, merge) hanya dipegang selama operasinya berjalan, tidak pernah sambil
   menunggu task anak (ADR-0010; mencegah deadlock spawn tree).
3. Task paralel wajib mendeklarasikan write set yang tidak beririsan (pola glob). Kalau beririsan → serial.
4. Merge queue serial: setiap kandidat di-rebase ke `main` terbaru dan dites ulang (ADR-0005).
5. Merge terjadi di integration worktree terpisah; ref `main` di-fast-forward paling akhir dengan
   `git update-ref <ref> <baru> <lama>` (CAS).
6. Setiap worker berjalan di process group sendiri; cancel = soft → terminate → kill.
7. Task non-idempoten yang crash masuk NEEDS_HUMAN, tidak pernah di-retry otomatis.

## Antarmuka: TUI sekarang, GUI nanti (ADR-0012)

Caecilia Harness adalah aplikasi lokal: semua state, worktree, dan kredensial ada di mesin pengguna.
Antarmukanya bertahap:

| Tahap | Frontend | Cara bicara ke core |
| --- | --- | --- |
| M1 | CLI (`caecilia run/status/resume`) | Panggilan fungsi langsung di proses yang sama |
| M7 | Retro Dark TUI (Textual) | Control API lokal (approve, cancel, subscribe event) + baca DB read-only |
| M8 (opsional) | GUI desktop berjendela | Control API lokal yang sama; tidak ada perubahan di core |

Aturan: `interfaces/` hanya berisi parsing input, format tampilan, dan implementasi `UserPrompter`.
Semua logika yang dibutuhkan lebih dari satu frontend berada di luar `interfaces/` (mis. `bootstrap.py`).

## Interaksi pengguna di M1

Karena hanya satu proses yang menulis DB, di M1 approval dan cancel dilakukan **di terminal yang
menjalankan `caecilia run`**: approval berupa prompt y/n, cancel lewat Ctrl+C (sekali = soft, dua kali
= hard). Proses lain hanya membaca (`caecilia status`). Perintah lintas proses (approve/cancel dari
terminal lain) menyusul di M7 lewat control API lokal, yang nanti juga dipakai GUI (ADR-0012).

## Serial vs paralel (hipotesis, wajib di-benchmark — backlog M2-B)

Waktu serial ≈ jumlah durasi semua task. Waktu paralel ≈ planning + critical path + jumlah waktu gate +
resolusi konflik. Paralel memakan lebih banyak token (setiap worker mendapat context pack sendiri).

Paralelkan hanya jika: ≥ 2 task READY, write set yang dideklarasikan tidak beririsan, setiap task
diperkirakan jauh lebih lama daripada satu siklus gate, dan budget token cukup untuk konteks ganda.
Selain itu jalankan serial.
