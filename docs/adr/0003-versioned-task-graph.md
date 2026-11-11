# ADR-0003: Task graph berversi dengan CAS

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Pekerjaan nyata menemukan task baru di tengah jalan. deepseek-harness mengizinkan dependensi diubah saat berjalan dan memvalidasi ulang siklus di setiap mutasi; DAG statis tidak bisa merepresentasikan ini.

## Keputusan

Task graph punya revisi. Planner menulis revisi 1. Worker hanya boleh mengusulkan task atau edge baru; orchestrator memvalidasi (hilang, duplikat, merujuk diri sendiri, siklus) lalu meng-commit dengan compare-and-set pada revisi.

## Alternatif yang ditolak

DAG statis yang dikunci saat planning; board bebas tanpa validasi.

## Konsekuensi

Perubahan graf yang butuh persetujuan harus muncul di antrean approval.
