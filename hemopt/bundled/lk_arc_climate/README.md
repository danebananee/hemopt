# LK Arc Climate — utförlig guide

Home Assistant-menyer nedan är på **engelska** (som i din installation).

## Vad du har idag

| Del | Vad den gör | Kan styra börvärde? |
| --- | --- | --- |
| **LK-appen** | Visar rum, du kan dra temperatur | Ja |
| **LK Systems** i HA | Temperature, Humidity, Battery, RSSI | **Nej** — bara sensorer |
| **LK Arc Climate** | Lägger till **Thermostat** på samma enhet | **Ja** — samma moln som appen |
| **hemopt** | Skriver börvärden när «Styr värmen» är på | Via `climate.*` |

## Installera (rekommenderat) — via hemopt-tillägget

Du behöver **inte** kopiera filer själv. Från version **0.1.20** lägger
hemopt in komponenten automatiskt. Från **0.1.24** skapas även
integrationen automatiskt efter **Restart Home Assistant** — du behöver
normalt **inte** «Add integration» manuellt.

1. **Settings → Add-ons → Kostnadsoptimering (hemopt)**
2. Tryck **Update** till **0.1.24+**, sedan **Restart**
3. Öppna fliken **Log** — du ska se:
   `LK Arc Climate installerad i /homeassistant/custom_components/lk_arc_climate`
4. **Settings → System → ⋮ → Restart Home Assistant** (en gång)
5. När HA + hemopt är uppe, kolla Logs efter:
   `LK Arc Climate check:` och `LK Arc Climate: termostat … → climate.…_thermostat`
6. **Golvvärme**-dashboarden ska visa termostater (inte Entity not found)

Om termostaterna fortfarande saknas (reserv):
**Settings → Devices & services → Add integration → LK Arc Climate**

### Kontroll

1. **Settings → Devices & services → LK Systems** → öppna t.ex. `c8:1b:04:e0:7e:90`
2. Där ska **Thermostat** synas (inte bara Sensors)
3. **Developer tools → States** → sök `climate.` och `_thermostat`

Exempel:

```text
climate.c8_1b_04_e0_7e_90_thermostat
```

### Om HA ändras men LK-appen inte gör det

Äldre byggen skrev till en Azure-endpoint som kunde se ut att lyckas utan att
appen uppdaterades. Från **0.1.21** används samma `link2.lk.nu`-anrop som appen
(`service/arc/sense/<mac>/measurement/true`). Från **0.1.23** verifieras att
molnet faktiskt fick det nya börvärdet innan HA visar OK.

Uppdatera hemopt → Restart tillägg → **Restart Home Assistant** → testa igen.
Kolla **Settings → System → Logs** efter `LK Arc Climate: OK — skrev`.

### Viktigt: ratt entitet

Andra **`climate.<mac>_thermostat`** (Thermostat pa rumsenheten).

**Inte** `sensor.hemopt_setpoint_*` — det ar bara hemopts *plan* (MQTT, lases
endast, heter «Plan (ej styrt) …» fran 0.1.23) och styr inte LK-appen.

I Logs ska du se (niva Warning):
`LK Arc Climate: forsoker satta …` och sedan `OK — skrev … (verifierat)`.

### MQTT-varning om `object_id`

Om Logs visar
`deprecated option object_id` for `sensor.hemopt_*` kor du en **aldre** hemopt
an 0.1.22. Tryck **Update** till 0.1.23+, **Restart** tillagget — da republiseras
discovery med `default_entity_id` och varningen forsvinner. Det har inget med
LK-appen att gora.

### Snabbtest

Ändra börvärdet i HA → öppna LK-appen → samma rum ska uppdateras inom några sekunder.

---

## Koppla till hemopt

I `/config/hemopt.yaml` (samma plats som `configuration.yaml`) ska varje rum ha:

```yaml
    climate_entity: climate.c8_1b_04_e0_7e_90_thermostat
```

Mallfilen i repot har redan rätt id för dina rum. Matcha mot
**Developer tools → States**.

Sedan: **Settings → Add-ons → Kostnadsoptimering → Restart**, och när planen
ser bra ut: slå på **Styr värmen**.

---

## Manuell installation (bara om tillägget inte kan skriva)

Om loggen säger att `/homeassistant` saknas:

1. Se till att tillägget har tillgång till Home Assistant-config
   (`homeassistant_config:rw` i manifestet — standard)
2. Eller kopiera mappen `custom_components/lk_arc_climate` från GitHub till
   `/config/custom_components/lk_arc_climate/` via Studio Code Server
3. **Restart Home Assistant**, sedan **Add integration → LK Arc Climate**

---

## Felsökning

| Symptom | Åtgärd |
| --- | --- |
| Ingen rad om LK Arc Climate i hemopt-loggen | Uppdatera till 0.1.20+, **Restart** tillägget, kolla Log igen |
| Integrationen syns inte under Add integration | Du glömde **Restart Home Assistant** efter att tillägget installerat filerna |
| *LK Systems is missing* | Installera/aktivera **LK Systems** (angoyd/ha-lksystems) först |
| Bara Sensors, ingen Thermostat | **LK Arc Climate → ⋮ → Reload**. Kolla Logs för `lk_arc_climate` |
| MQTT `deprecated option object_id` for sensor.hemopt_* | Du kör &lt; 0.1.22 — **Update** till 0.1.23+, Restart tillägget |
| Ändring syns inte i LK-appen | Styra `climate.*_thermostat`, inte `sensor.hemopt_setpoint_*`. Logs: `LK Arc Climate:` |

## Kort checklista

- [ ] hemopt 0.1.23+ uppdaterad och omstartad
- [ ] Loggen visar att LK Arc Climate installerats
- [ ] **Restart Home Assistant** en gång
- [ ] **Add integration → LK Arc Climate**
- [ ] `climate.*_thermostat` syns i States
- [ ] Test HA → LK-appen
- [ ] `climate_entity` i `hemopt.yaml` + hemopt restart + **Styr värmen**
