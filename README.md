# Brezdomko

Spremlja [nepremicnine.net](https://www.nepremicnine.net) in nove oglase, ki ustrezajo
zahtevam posameznega iskanja (»alert«), pošlje v njegov Discord kanal. Vsak alert ima svoje
zahteve, iskalne strani in Discord webhook — npr. `ljubljana` (3-sobno za tri) in `maribor`.

Cron vsakih 30 minut (7:00–23:30) za vsak alert zažene Claude Code brez vmesnika. Claude
pregleda nove oglase, za obetavne prebere celoten oglas in pogleda fotografije (npr. dejansko
število spalnic — »3-sobno« šteje tudi dnevno sobo) ter ujemanja pošlje v Discord s kratko
utemeljitvijo, ceno in fotografijo.

## Datoteke

| Pot | Kaj je |
|---|---|
| `config/alerts/<ime>/alert.json` | Discord webhook in iskalne strani alerta (ni v gitu). |
| `config/alerts/<ime>/criteria.md` | Zahteve alerta — berejo se ob vsakem zagonu (ni v gitu). |
| `config/alerts/example/` | Predloga za nov alert. |
| `.claude/skills/nepremicnine-scout/SKILL.md` | Postopek, ki mu sledi Claude. |
| `.claude/skills/nepremicnine-scout/nep.py` | Dostop do strani in Discorda (`alerts`, `scan`, `list`, `detail`, `notify`, `mark-seen`). |
| `run.sh` | Vstopna točka za cron (Sonnet, omejitev 2 $ na zagon alerta). |
| `data/<ime>/seen.json` | Že obdelani oglasi alerta — prepreči podvojena obvestila (ni v gitu). |
| `logs/` | Povzetek vsakega zagona (`<datum>_<ime>.log`); hrani se 14 dni. |

Stran je za Cloudflare zaščito, zato `nep.py` uporablja pravi Chrome v navideznem zaslonu
(`xvfb-run`).

## Namestitev

Potrebuješ Linux, Google Chrome, `xvfb-run`, `uv` in prijavljen Claude Code.

```bash
(crontab -l; echo "*/30 7-23 * * * $PWD/run.sh >> $PWD/logs/cron.log 2>&1") | crontab -
```

## Nov alert

```bash
cp -r config/alerts/example config/alerts/<ime>
# alert.json: vpiši discord_webhook_url (Discord → Integrations → Webhooks) in iskalne URL-je
# criteria.md: opiši, kaj iščemo (v jeziku, v katerem želiš obvestila)
uv run .claude/skills/nepremicnine-scout/nep.py alerts     # preveri, da je "ready"
```

Cron ga pobere ob naslednjem zagonu. Prvi zagon pregleda vse oglase na prvih straneh.

## Uporaba

`nep.py` ukazi potrebujejo `--alert <ime>`; spodaj `NEP` = `uv run .claude/skills/nepremicnine-scout/nep.py --alert <ime>`.

- **Spremeni zahteve ali iskanja:** uredi datoteki v `config/alerts/<ime>/`.
- **Zaženi takoj:** `./run.sh` (vsi alerti) ali `./run.sh <ime>`.
- **Za nazaj (zadnjih 1/2/3/7 dni):**
  ```bash
  xvfb-run -a NEP scan --last 7
  claude -p "Run the nepremicnine-scout skill for alert <ime>, but skip the scan step." --model sonnet --permission-mode dontAsk
  ```
- **Od začetka:** `echo '{}' > data/<ime>/seen.json` (lahko ponovno pošlje stara ujemanja).
- **Premor:** zakomentiraj vrstico v `crontab -e`; za en alert izprazni `discord_webhook_url`.

Računalnik mora biti prižgan; zamujeni zagoni se ne nadoknadijo. Zagoni porabljajo
naročnino, s katero je prijavljen Claude Code (razen če je nastavljen `ANTHROPIC_API_KEY`).
