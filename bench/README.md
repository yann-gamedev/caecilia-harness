# Benchmark serial vs paralel (M2-B)

Alat ini mengukur kapan menjalankan task secara paralel benar-benar lebih cepat daripada serial, dan
berapa biayanya. Hasilnya dipakai untuk keputusan D11 (`max_parallel` default) di
`docs/decisions-open.md`.

## Workload

| ID | Isi | Task | Dependensi |
| --- | --- | --- | --- |
| W1 | dua paket independen (`api/`, `web/`) | 4 | tidak ada |
| W2 | ganti nama fungsi, perbarui pemanggil, tambah tes | 3 | rantai |
| W3 | 6 edit kecil di modul terpisah | 6 | tidak ada |

Setiap run memakai `CAECILIA_HOME` dan repo baru di folder sementara, dengan `--auto-merge`. Check
setiap repo adalah tes `unittest`-nya sendiri.

Perintah yang diminta model **disetujui otomatis hanya kalau programnya `python`, `py`, `uv`, atau
`pytest`**; semua perintah lain ditolak. Daftar ini juga dicetak di awal setiap benchmark.

## Uji cepat tanpa biaya

```sh
uv run python -m bench.run_bench --workload W1 --mode parallel --worker fake --runs 1
```

Worker palsu tidak memanggil model, jadi token dan biayanya 0. Ini hanya memeriksa alatnya.

## Bagian B: run dengan model sungguhan (dijalankan pemilik)

> **Biaya.** Satu putaran lengkap di bawah = 3 workload × 2 mode × 5 run = **30 run**, kira-kira
> **130 eksekusi task** oleh model (W1: 4, W2: 3, W3: 6 task per run). Biayanya bergantung pada model dan
> tarif akun DeepSeek Anda. Coba dulu dengan `--runs 1` untuk satu workload dan lihat token yang tercatat.

1. Simpan API key sekali: `uv run caecilia key set deepseek`.
2. Tentukan nama file hasil, misalnya `bench/results/2026-11-30.jsonl`.
3. Jalankan (PowerShell; ganti `<model>`, serta isi harga per 1 juta token kalau ingin biaya dihitung):

```powershell
$out = "bench/results/2026-11-30.jsonl"
$common = "--worker", "native", "--model", "<model>", "--runs", "5", "--out", $out,
          "--price-in", "<USD per 1M token input>", "--price-out", "<USD per 1M token output>"
foreach ($w in "W1", "W2", "W3") {
  uv run python -m bench.run_bench --workload $w --mode serial @common
  uv run python -m bench.run_bench --workload $w --mode parallel --max-parallel 2 @common
}
```

Kalau ingin membandingkan beberapa nilai `max_parallel`, ulangi baris `parallel` dengan
`--max-parallel 3` ke file yang sama; laporan memisahkan setiap nilai.

Setiap run ditambahkan sebagai satu baris JSON segera setelah selesai, jadi benchmark yang terhenti di
tengah tetap menyimpan run yang sudah selesai. Hentikan dengan Ctrl+C kapan saja.

## Laporan

```sh
uv run python -m bench.report bench/results/2026-11-30.jsonl
```

Tabel berisi median dan rentang (min..maks) wall time, token, dan biaya per workload × mode, jumlah
`gate.failed`, REWORK, NEEDS_HUMAN, run yang tidak `done`, dan run yang check-nya gagal di head target,
plus rasio paralel/serial. Setelah file hasil disimpan, agent menulis laporan lengkap (bagian C,
`docs/benchmarks/`).

## Metrik per run (satu baris JSONL)

`wall_time_s` (`run.created` → `run.finished`), `task_durations_s` (lease pertama → state terakhir per
task), `input_tokens`/`output_tokens` (jumlah `usage.recorded`), `cost_usd` (kalau harga diberikan),
`gate_failed`, `rework`, `needs_human`, `status`, `check_passed_after`, `commands_allowed`/
`commands_refused`, serta `harness_version`, `harness_commit`, `model`, `workload`, `mode`,
`max_parallel`.
