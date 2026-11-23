# Spesifikasi Retro Dark TUI

Disalin dari audit arsitektur v0.2 (bagian F) agar bisa dibaca agent. Dipakai di M7-02 dan, untuk token
warna, di GUI (M8) kalau desain pemilik memakai tema yang sama.

## Prinsip

- TUI adalah *view* atas control API dan DB read-only; tidak memegang state sendiri.
- Status tidak pernah hanya dibedakan warna: selalu glyph + kata.
- Semua aksi bisa lewat keyboard **dan** lewat CLI/control API.

## Token warna

| Token | Hex | Kontras vs canvas #0B0F0E | Kontras vs panel #121A18 | Dipakai untuk |
| --- | --- | --- | --- | --- |
| Canvas | #0B0F0E | — | — | latar |
| Panel | #121A18 | — | — | latar panel |
| Text | #DCE5D8 | 14,9:1 | 13,7:1 | isi utama |
| Muted | #85968D | 6,2:1 | 5,7:1 | metadata, waktu |
| Phosphor | #A4E56A | 12,9:1 | 11,8:1 | RUNNING, fokus |
| Cyan | #65C8C0 | 9,7:1 | 8,9:1 | info, link, DONE |
| Amber | #E8B86B | 10,6:1 | 9,7:1 | menunggu, approval |
| Error | #E07770 | 6,5:1 | 5,9:1 | FAILED, NEEDS_HUMAN |

## Glyph status

`[ ] PEND`, `[.] READY`, `[>] RUN`, `[~] GATE`, `[?] ASK`, `[!] FAIL`, `[x] DONE`, `[-] CXL`, `[*] STALE`,
`[h] HUMAN`. Tambahkan glyph untuk state baru di `core/states.py` (tes memastikan semuanya punya glyph).

## Wireframe (≥ 160 kolom)

```text
+-- CAECILIA :: <repo> ---- run_01 ---- 2 RUN  1 GATE  1 ASK  0 FAIL ---- 41k/2M tok ---- 14:02 --+
| TASK GRAPH (rev 5)    | SESSION: api  [>] RUN  wt/api-1               | INSPECTOR                 |
| [x] contract          | > read api/openapi.yaml (rev 2)               | Context pins              |
| [>] web          *    | > edit web/src/client.ts                      |  contract:api  r2  ok     |
| [~] api               | $ npm test  (approval: ya)                    | Write set: web/**         |
| [ ] integrate         |   12 passed, 0 failed                         | Tokens 41k in / 6k out    |
|     <- web, api       |                                               | Attempt 1 / 3             |
+-----------------------+-----------------------------------------------+---------------------------+
| [?] ASK  api ingin menjalankan: git push origin ...    [a]llow  [d]eny  [v]iew                     |
| EVENTS  14:01:58 document.updated contract:api r2 | 14:02:03 web context_stale -> rerun            |
| caecilia> _                                                                                        |
+----------------------------------------------------------------------------------------------------+
```

## Layout responsif

| Lebar terminal | Layout |
| --- | --- |
| ≥ 160 kolom | graf, sesi, inspector, bar approval + ticker + input |
| 120–159 | graf + sesi; inspector jadi tab |
| 80–119 | satu panel utama + sidebar graf yang bisa dilipat (`g`) |
| < 80 atau < 24 baris | sarankan `caecilia --simple` (mode baris) |

## Keybinding default (`CAECILIA_HOME/keymap.toml` bisa mengubahnya)

| Tombol | Aksi |
| --- | --- |
| `Ctrl+P` | command palette |
| `j`/`k`, `↑`/`↓` | pindah task |
| `Enter` | fokus sesi task terpilih |
| `Tab` / `Shift+Tab` | pindah panel |
| `a` / `d` / `v` | allow / deny / lihat detail approval teratas |
| `c` / `C` | cancel task / cancel run (dengan konfirmasi) |
| `p` | pause/resume di batas step |
| `f` | follow log |
| `/` | cari di log |
| `g` | lipat panel graf |
| `?` | bantuan |
| `i` | fokus input goal |

Mode vim opsional, mati default.

## Performa dan aksesibilitas

- Render ±15 fps maksimal, event di-batch; tidak ada render per token; log per sesi di ring buffer.
- `--plain` (log tanpa warna dan box drawing), `--simple` (mode baris), tema high-contrast,
  `--reduced-motion`. Efek CRT opsional, mati default.
