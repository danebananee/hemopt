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
hemopt in komponenten automatiskt i Home Assistants config.

1. **Settings → Add-ons → Kostnadsoptimering (hemopt)**
2. Tryck **Update** (till 0.1.20+) om den finns, annars **Rebuild** / **Restart**
3. Öppna fliken **Log** — du ska se något i stil med:
   `LK Arc Climate installerad i /homeassistant/custom_components/lk_arc_climate`
4. **Settings → System → ⋮ → Restart Home Assistant** (en gång — krävs för
   att HA ska hitta den nya custom componenten)
5. När HA är uppe: **Settings → Devices & services → Add integration**
6. Sök **LK Arc Climate** → lägg till (inget lösenord — återanvänder LK Systems)

### Kontroll

1. **Settings → Devices & services → LK Systems** → öppna t.ex. `c8:1b:04:e0:7e:90`
2. Där ska **Thermostat** synas (inte bara Sensors)
3. **Developer tools → States** → sök `climate.` och `_thermostat`

Exempel:

```text
climate.c8_1b_04_e0_7e_90_thermostat
```

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
| Ändring syns inte i LK-appen | Vänta 10–30 s; kolla Logs |

## Kort checklista

- [ ] hemopt 0.1.20+ uppdaterad och omstartad
- [ ] Loggen visar att LK Arc Climate installerats
- [ ] **Restart Home Assistant** en gång
- [ ] **Add integration → LK Arc Climate**
- [ ] `climate.*_thermostat` syns i States
- [ ] Test HA → LK-appen
- [ ] `climate_entity` i `hemopt.yaml` + hemopt restart + **Styr värmen**
