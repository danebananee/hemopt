# LK Arc Climate

Skapar **skrivbara** `climate.*`-entiteter för LK Arc Sense-rumstermostater.

## Varför behövs den?

[angoyd/ha-lksystems](https://github.com/angoyd/ha-lksystems) ger dig ofta bara
sensorer (temperatur, fukt, batteri, RSSI) för `arc-sense`. Climate-plattformen
där skapas bara när molnet sätter `deviceRole: arc-tune` — många hus (inklusive
när termostaten går att ställa i LK-appen) får aldrig den rollen.

Den här companion-integrationen:

1. Återanvänder din befintliga **LK Systems**-inloggning (ingen ny lösenord)
2. Skapar en `climate.<rum>` per Arc Sense
3. Skriver börvärde via samma LK-moln-API som appen

## Installera

1. Kopiera mappen `custom_components/lk_arc_climate` till Home Assistants
   `/config/custom_components/lk_arc_climate`
   (Samba, Studio Code Server, eller `scp`)
2. **Starta om** Home Assistant
3. **Inställningar → Enheter och tjänster → Lägg till integration → LK Arc Climate**
4. Bekräfta — klart

Du ska nu se t.ex. `climate.tvattstuga` under respektive rumsenhet.

## Koppla till hemopt

I `/config/hemopt.yaml`, sätt per rum:

```yaml
rooms:
  - key: tvattstuga
    name: Tvattstuga
    temperature_entity: sensor.c8_1b_04_e0_7e_90_temperature
    climate_entity: climate.tvattstuga   # från den här integrationen
```

Kontrollera exakt entitets-id under **Utvecklarverktyg → Tillstånd** (sök `climate.`).

## Felsökning

| Symptom | Åtgärd |
| --- | --- |
| Integration saknas efter omstart | Kontrollera sökvägen `custom_components/lk_arc_climate/manifest.json` |
| «LK Systems saknas» | Installera/starta angoyd/ha-lksystems först |
| Inga climate-entiteter | LK Systems → ⋮ → Ladda om; kolla Log för `lk_arc_climate` |
| Setpoint skrivs inte | Testa att ändra i HA UI; kolla Log. MAC måste nå Azure-endpointen |

## Licens

Samma som hemopt-repot (MIT). API-anrop speglar angoyd/ha-lksystems.
