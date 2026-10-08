# Brezdomko

Spremlja [nepremicnine.net](https://www.nepremicnine.net) in nove oglase za najem stanovanj
v Ljubljani, ki ustrezajo našim zahtevam, pošlje v Discord kanal.

Cron vsakih 30 minut (7:00–23:30) zažene Claude Code brez vmesnika. Claude pregleda nove
oglase, za obetavne prebere celoten oglas (npr. dejansko število spalnic — »3-sobno« šteje
tudi dnevno sobo) in ujemanja pošlje v Discord s kratko utemeljitvijo, ceno in fotografijo.

## Datoteke

| Pot | Kaj je |
|---|---|
| `config/criteria.md` | Naše zahteve — urejaj tukaj, berejo se ob vsakem zagonu (ni v gitu; predloga: `criteria.example.md`). |
| `.claude/skills/nepremicnine-scout/SKILL.md` | Postopek, ki mu sledi Claude. |
| `config/searches.json` | Katere iskalne strani spremljamo. |
| `.claude/skills/nepremicnine-scout/nep.py` | Dostop do strani in Discorda (`scan`, `list`, `detail`, `notify`, `mark-seen`). |
| `run.sh` | Vstopna točka za cron (Sonnet, omejitev 2 $ na zagon). |
| `.env` | `DISCORD_WEBHOOK_URL` (ni v gitu). |
| `data/seen.json` | Že obdelani oglasi — prepreči podvojena obvestila (ni v gitu). |
| `logs/` | Povzetek vsakega zagona; hrani se 14 dni. |

Stran je za Cloudflare zaščito, zato `nep.py` uporablja pravi Chrome v navideznem zaslonu
(`xvfb-run`).

## Namestitev

Potrebuješ Linux, Google Chrome, `xvfb-run`, `uv` in prijavljen Claude Code.

```bash
cp .env.example .env     # vpiši DISCORD_WEBHOOK_URL (Discord → Integrations → Webhooks)
cp config/criteria.example.md config/criteria.md   # opiši, kaj iščemo
xvfb-run -a uv run .claude/skills/nepremicnine-scout/nep.py scan    # preizkus
(crontab -l; echo "*/30 7-23 * * * $PWD/run.sh >> $PWD/logs/cron.log 2>&1") | crontab -
```

Računalnik mora biti prižgan; zamujeni zagoni se ne nadoknadijo.

## Uporaba

- **Spremeni zahteve:** uredi `config/criteria.md`.
- **Dodaj območje ali tip nepremičnine:** nov vnos v `config/searches.json`.
- **Zaženi takoj:** `./run.sh`
- **Za nazaj (zadnjih 1/2/3/7 dni):**
  ```bash
  xvfb-run -a uv run .claude/skills/nepremicnine-scout/nep.py scan --last 7
  claude -p "Run the nepremicnine-scout skill, but skip the scan step." --model sonnet --permission-mode dontAsk
  ```
- **Od začetka:** `echo '{}' > data/seen.json` (lahko ponovno pošlje stara ujemanja).
- **Premor:** zakomentiraj vrstico v `crontab -e`.

Zagoni porabljajo naročnino, s katero je prijavljen Claude Code (razen če je nastavljen
`ANTHROPIC_API_KEY`).
