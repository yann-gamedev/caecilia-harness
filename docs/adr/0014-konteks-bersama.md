# ADR-0014: Konteks bersama — dokumen berversi, pin, dan invalidasi

- Status: Diterima
- Tanggal: 2026-11-23

## Konteks

Worker paralel butuh pengetahuan bersama (kontrak API, keputusan, catatan desain) tanpa saling membaca
transkrip. Skema M0 sudah punya tabel `documents` dan `context_pins`, state STALE, dan
`StateStore.cas_document`, tetapi belum ada yang memakainya. M2 juga menunda dua hal ke M3: worker
mengusulkan task/edge baru (ADR-0003) dan menaikkan budget run yang sudah habis.

## Keputusan

1. **Dokumen** adalah teks berversi milik run: `id` (`[a-z0-9_-:]+`, mis. `contract:api`), `kind`
   (`contract`, `decision`, `note`, `spec`), `revision`, isi disimpan sebagai artefak content-addressed.
   Event `document.created` dan `document.updated` di stream `doc:<run_id>:<id>`; setiap update memakai
   CAS revisi (`expected_revision`).
2. **Pin:** task menyebut dokumen yang dibutuhkannya (`context = [...]` di plan). Saat spawn, revisi
   terbaru setiap dokumen dicatat di `context_pins` dan di payload `worker.started`. Context pack hanya
   berisi dokumen yang di-pin, handoff note, dan ringkasan blocker; **tidak pernah** transkrip worker lain.
3. **Invalidasi:** dokumen naik revisi → setiap task non-terminal yang mem-pin revisi lama mendapat
   `task.context_stale`. RUNNING → worker dibatalkan halus lalu STALE → READY (jalan ulang dengan konteks
   baru). AWAITING_GATE → STALE → REWORK (diff lama ikut sebagai handoff). Task yang sudah INTEGRATING
   dibiarkan selesai, karena pin-nya masih terbaru saat gate dimulai. Tepat sebelum AWAITING_GATE →
   INTEGRATING, orchestrator memeriksa ulang pin.
4. **Usulan dari worker:** worker tidak pernah mengubah dokumen atau graf secara langsung. Tool
   `propose_document_update`, `propose_task`, dan `propose_dependency` menghasilkan usulan yang dicatat sebagai
   event, divalidasi orchestrator (CAS dokumen; validasi graf penuh), dan diterapkan hanya setelah approval
   (`ask` secara default). Karena orchestrator adalah satu-satunya penulis, usulan boleh diterapkan saat
   run berjalan.
5. **Ringkasan handoff:** saat task DONE, ringkasan worker + daftar file yang berubah disimpan sebagai
   event `task.handoff`. Task dependen menerima handoff blocker langsungnya di context pack.
6. **Ukuran:** context pack dibatasi (default 60.000 karakter). Kalau terlampaui, dokumen dipotong dengan
   penanda yang menyebut id dan revisi, dan kejadian itu dicatat.

## Alternatif yang ditolak

- Memberi worker akses ke transkrip worker lain: boros token, membocorkan isi tak tepercaya antar task.
- Worker menulis dokumen langsung: melanggar aturan satu penulis dan melewati approval.
- Membatalkan task INTEGRATING saat dokumen berubah: tabel state tidak mengizinkannya dan merge yang
  sedang berjalan sudah memakai revisi yang benar saat itu.

## Konsekuensi

- Jalur STALE yang sudah ada mulai dipakai, dengan satu tambahan di tabel state: STALE → NEEDS_HUMAN,
  untuk task yang sudah 3 kali basi dalam satu run (mencegah loop tanpa akhir).
- Ada jenis approval baru (`document_update`, `graph_change`) yang tampil di prompter yang sama.
- Dokumen yang sering berubah membuat task sering jalan ulang; dicatat di event agar terlihat di benchmark.
