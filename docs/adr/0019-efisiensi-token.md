# ADR-0019: Efisiensi token — "cara kerja senior developer"

- Status: Diterima
- Tanggal: 2026-11-24

## Konteks

Pemilik ingin harness bekerja seperti senior developer: berpikir dulu, mengubah seperlunya, membuktikan
dengan tes, dan hemat token. Orca memakai agent LLM sebagai koordinator yang terus membaca laporan; itu
fleksibel tetapi setiap putaran koordinasi memakan token.

## Keputusan

1. **Koordinasi di kode, bukan di LLM.** Model dipakai untuk pekerjaan yang butuh penilaian: merencanakan
   (sekali per run, plus revisi), mengerjakan task, dan (opsional) review. Penjadwalan, retry, gate,
   recovery, dan pemantauan dilakukan kode deterministik tanpa token.
2. **Peran model terpisah.** `.caecilia.toml` dan plan boleh menentukan model/agent per peran: `planner`
   (model kuat), `worker` (default), `reviewer` (opsional). Task kecil boleh memakai model lebih murah.
3. **Konteks sempit.** Worker menerima: instruksi task, acceptance, write set, dokumen yang di-pin, handoff
   blocker, dan peta repo ringkas. Tidak pernah transkrip worker lain. Worker membaca file per rentang baris
   dan mencari dengan tool `search`, bukan membaca seluruh repo.
4. **Bukti dari mesin, bukan dari model.** Check command (tes, build, lint) adalah hakim. Model tidak diminta
   "memeriksa ulang" hasil yang sudah dibuktikan tes; rework hanya membawa output gagal yang relevan.
5. **Laporan pendek dan terstruktur** (tiga kalimat + field terstruktur), sehingga tidak ada model yang harus
   membaca log panjang.
6. **Prefix prompt stabil** (system prompt, definisi tool, lalu konteks berversi, lalu instruksi task) supaya
   cache prompt di sisi provider bisa terpakai.
7. **Token diukur per peran dan per task**, tampil di `status`, dan menjadi metrik benchmark.

## Konsekuensi

- Harness tidak bisa "berimprovisasi" mengubah rencana tanpa revisi graf; perubahan rencana lewat usulan
  worker (prompt 27) atau pengguna.
- Planner yang buruk menghasilkan run yang buruk; karena itu plan selalu ditampilkan untuk disetujui.
