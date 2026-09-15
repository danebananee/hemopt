# LK Arc Climate — utförlig guide

Home Assistant-menyer nedan är på **engelska** (som i din installation).

## Vad du har idag

| Del | Vad den gör | Kan styra börvärde? |
| --- | --- | --- |
| **LK-appen** | Visar rum, du kan dra temperatur (t.ex. Tvättstuga 20,0 °C) | Ja |
| **LK Systems** i HA ([angoyd/ha-lksystems](https://github.com/angoyd/ha-lksystems)) | Visar Temperature, Humidity, Battery, RSSI på enheten `c8:1b:04:e0:7e:90` m.fl. | **Nej** — bara sensorer |
| **LK Arc Climate** (den här mappen) | Lägger till en **Thermostat** på samma enhet | **Ja** — samma moln som appen |
| **hemopt** | Läser sensorer, skriver börvärden när «Styr värmen» är på | Via `climate.*` |

Stock-integrationen skapar `climate.*` bara om molnet säger `deviceRole: arc-tune`.
Hos dig (och många andra) syns bara `arc-sense` + sensorer, trots att appen
kan ställa temperatur. Därför behövs companion-integrationen.

## Förutsättningar

1. **LK Systems** redan tillagd i HA och inloggad med **samma konto som LK-appen**
2. Rumsenheter synliga under **Settings → Devices & services → LK Systems**
3. Du kan kopiera filer till `/config` (Samba share eller **Studio Code Server**)

Om LK Systems saknas: installera den via HACS först (custom repository
`https://github.com/angoyd/ha-lksystems`), starta om HA, lägg till integrationen
och logga in.

---

## Del 1 — Installera LK Arc Climate

### 1. Hämta filerna

Från GitHub-repot [danebananee/hemopt](https://github.com/danebananee/hemopt):

- Mappen `custom_components/lk_arc_climate/` (hela mappen, med
  `manifest.json`, `climate.py`, osv.)

### 2. Kopiera in i Home Assistant

Målet ska bli exakt:

```text
/config/custom_components/lk_arc_climate/manifest.json
/config/custom_components/lk_arc_climate/__init__.py
/config/custom_components/lk_arc_climate/climate.py
… (övriga filer i mappen)
```

**Via Studio Code Server (enklast):**

1. **Settings → Add-ons → Add-on Store** → sök *Studio Code Server* →
   **Install** → **Start** → **Open Web UI**
2. I filträdet: öppna `config` → skapa mappen `custom_components` om den
   saknas → skapa `lk_arc_climate` under den
3. Ladda upp / klistra in alla filer från repots
   `custom_components/lk_arc_climate/`

**Via Samba share:**

1. Installera tillägget **Samba share**, aktivera share för `config`
2. Från datorn: kopiera hela mappen `lk_arc_climate` till
   `\\homeassistant\config\custom_components\`

Kontroll: filen `manifest.json` ska innehålla `"domain": "lk_arc_climate"`.

### 3. Starta om Home Assistant

**Settings → System → Hardware** (eller **Settings → System**) →
trepunktsmenyn uppe till höger → **Restart Home Assistant** → **Restart**.

Vänta tills HA är uppe igen (1–2 minuter).

### 4. Lägg till integrationen

1. **Settings → Devices & services**
2. **+ Add integration** (nere till höger)
3. Sök **LK Arc Climate**
4. Välj den och bekräfta

Den frågar **inte** efter lösenord — den återanvänder LK Systems-inloggningen.

Om du får *«LK Systems is missing»*: gå tillbaka och se till att
**LK Systems** finns under **Devices & services** och inte är disabled.

### 5. Kontrollera att termostaten finns

1. **Settings → Devices & services → LK Systems**
2. Öppna en rumsenhet (t.ex. den som heter `c8:1b:04:e0:7e:90`)
3. Under **Controls** (eller bland entiteter) ska du nu se **Thermostat**
4. Alternativt: **Developer tools → States**, sök `climate.` — du ska se
   entiteter i stil med:

   ```text
   climate.c8_1b_04_e0_7e_90_thermostat
   climate.fa_6a_ee_c8_9a_63_thermostat
   …
   ```

### 6. Snabbtest mot LK-appen

1. I HA: öppna termostaten och sätt t.ex. **21,0 °C**
2. Öppna **LK-appen** på telefonen för samma rum
3. Börvärdet ska uppdateras (kan ta några sekunder)

Om appen inte ändras: se **Felsökning** längst ner.

---

## Del 2 — Koppla till hemopt

hemopt skriver bara till entiteter som står i `/config/hemopt.yaml`.

### 1. Se vilka climate-id du fick

**Developer tools → States** → filtrera `climate.` och `_thermostat`.

MAC i enhetsnamnet `c8:1b:04:e0:7e:90` blir normalt:

```text
climate.c8_1b_04_e0_7e_90_thermostat
```

(kolon → understreck, suffix `_thermostat`).

### 2. Uppdatera `hemopt.yaml`

Exempel för ett rum (Tvättstuga):

```yaml
  - key: tvattstuga
    name: Tvattstuga
    floor: Bottenvaning
    priority: 1
    temperature_entity: sensor.e0_ec_2c_c8_5e_2c_temperature
    humidity_entity: sensor.e0_ec_2c_c8_5e_2c_humidity
    climate_entity: climate.e0_ec_2c_c8_5e_2c_thermostat
    comfort_min: 18.0
    comfort_max: 21.0
    heat_share: 0.8
```

Mallfilen i repot (`hemopt/hemopt.init.yaml`) har redan `climate_entity` ifyllda
för dina rum — jämför med **Developer tools** och rätta om något MAC skiljer sig.

### 3. Starta om hemopt

**Settings → Add-ons → Kostnadsoptimering (hemopt) → Restart**.

### 4. Doctor

I hemopt-panelen (eller loggen): kör / kolla doctor så att
`climate.*_thermostat` inte står som saknade.

### 5. Styrning

1. Låt hemopt planera några cykler med styrning **av**
2. När kurvan ser rimlig ut: slå på **Styr värmen**
3. hemopt skriver då börvärde per rum via `climate_entity`

**Utan** `climate_entity` (eller om LK Arc Climate inte är installerad) faller
hemopt tillbaka till husnivå:
`heat_pump.room_setpoint_entity` → `climate.h66_hproom_temp_setpoint`
(hela huset, inte rum för rum).

---

## Del 3 — Dashboard (valfritt)

Filen `dashboards/golvvarme.yaml` i repot har tile-kort med
`climate.<mac>_thermostat`. Importera den när termostaterna finns, annars
visar korten *Entity not found*.

---

## Felsökning

| Symptom | Vad du gör |
| --- | --- |
| **LK Arc Climate** syns inte under Add integration | Omstart saknas, eller fel sökväg. Kontrollera att `manifest.json` ligger i `/config/custom_components/lk_arc_climate/` |
| Abort: *LK Systems is missing* | **Settings → Devices & services** — lägg till / aktivera **LK Systems** först |
| Enheten har fortfarande bara Sensors, ingen Thermostat | **Settings → Devices & services → LK Arc Climate → ⋮ → Reload**. Saknas integrationen helt: lägg till den igen. Kolla **Settings → System → Logs** och sök `lk_arc_climate` |
| `climate.*_thermostat` saknas i States | Reload som ovan. Bekräfta att LK Systems ser rumsenheterna |
| Ändring i HA syns inte i LK-appen | Vänta 10–30 s. Testa att ändra i appen → läs av i HA. Vid fel: Logs för både `lksystems` och `lk_arc_climate` |
| hemopt skriver inte till rummen | `climate_entity` i `hemopt.yaml` måste matcha exakt id i States. **Styr värmen** måste vara på. Kolla hemopt-loggen |
| Entity not found på golvvärme-dashboard | Installera LK Arc Climate först, eller ta bort climate-tiles tills dess |

### Ladda om utan full HA-restart

Efter filändringar i custom component: full **Restart** behövs oftast första
gången. Därefter räcker ofta:

**Settings → Devices & services → LK Arc Climate → ⋮ → Reload**

---

## Kort checklista

- [ ] LK Systems installerad och inloggad (samma konto som appen)
- [ ] Mappen `lk_arc_climate` under `/config/custom_components/`
- [ ] Home Assistant omstartad
- [ ] Integrationen **LK Arc Climate** tillagd
- [ ] `climate.<mac>_thermostat` syns i **Developer tools → States**
- [ ] Test: ändra börvärde i HA → syns i LK-appen
- [ ] `climate_entity` ifylld per rum i `/config/hemopt.yaml`
- [ ] hemopt omstartad, sedan **Styr värmen** när du är nöjd med planen
