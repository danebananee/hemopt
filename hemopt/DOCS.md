# Kostnadsoptimering (hemopt)

Flyttar uppvärmning och varmvatten till billiga timmar, kapar effekttoppar och
jämför löpande vad de olika elavtalen skulle ha kostat dig.

Menyvägarna nedan står på engelska, eftersom Home Assistant är på engelska.

## Innan du börjar

Du behöver **Mosquitto broker**. Har du redan H66 på MQTT så har du den.
Annars: **Settings → Add-ons → Add-on Store**, sök *Mosquitto broker*,
**Install**, **Start**. Gå sedan till **Settings → Devices & Services** och
bekräfta MQTT-integrationen som dyker upp under *Discovered*.

## Installera

### A. Från GitHub (rekommenderat)

Repot måste vara **publikt**, eftersom Home Assistant klonar det anonymt. I
gengäld dyker nya versioner upp som en **Update**-knapp på tilläggets sida.

1. **Settings → Add-ons → Add-on Store**
2. Trepunktsmenyn uppe till höger → **Repositories**
3. Klistra in `https://github.com/danebananee/hemopt`, **Add**, **Close**
4. Ladda om sidan. Under rubriken med repots namn finns
   **Kostnadsoptimering (hemopt)**

Home Assistant bygger avbilden på din Raspberry Pi första gången, vilket tar
några minuter. Det är normalt att **Install** står och snurrar under tiden.

### B. Lokalt tillägg

Går utan GitHub, men då finns ingen **Update**-knapp.

Home Assistant letar efter tillägg i `/addons/<namn>/config.yaml`. Lägg mappen
`hemopt/` där, alltså som `/addons/hemopt/`, så dyker det upp av sig självt.

Filerna får du dit med **Samba share** eller **Studio Code Server**, båda finns
i Add-on Store. Kopiera hela `hemopt/`-mappen, inte repots rot.

Gå sedan till **Settings → Add-ons → Add-on Store**, tryck trepunktsmenyn och
**Check for updates**. Tillägget hamnar under **Local add-ons**. Uppdatering
görs genom att kopiera in mappen igen och trycka **Rebuild**.

### Sedan, oavsett väg

5. **Install**
6. Fliken **Configuration**: välj elområde, avräkning, effektregler, elmätare
   och MQTT (`core-mosquitto` + eventuellt user/lösen), tryck **Save**
7. Fliken **Info**: slå på **Start on boot**, **Watchdog** och
   **Show in sidebar**, tryck **Start**
   (eller **Rebuild** efter uppdatering)
8. Öppna **Kostnadsoptimering** i vänstermenyn

Ingen Info-toggle för API — `homeassistant_api` ges automatiskt. Om Loggen
visar `SUPERVISOR_TOKEN length=0` behövs en long-lived **HA-token** under
Configuration. MQTT-lösen behövs bara om Mosquitto kräver inloggning och
Supervisorn inte lämnat över broker-uppgifter.

## Vad tillägget redan vet

| Sak | Varifrån |
| --- | --- |
| Home Assistant-API | `SUPERVISOR_TOKEN`, ges av Supervisorn (annars HA-token) |
| MQTT-broker | Supervisorns tjänste-API, annars Configuration (`mqtt_host` …) |
| Lagring | `/data`, överlever uppdateringar |

## Vad som styrs (inte bara rum)

Optimeraren planerar **värmepumpens effekt**, **varmvattenladdning** efter
inlärt användningsmönster, och **rummens börvärden**. Styrningen går via
Home Assistant-entiteter (climate / number) — ofta MQTT→H66 under huven.
Slå på **Styr värmen** i panelen för att skriva börvärden; annars syns bara
planen. Under **Vad som styrs** syns vilka entiteter som är kopplade.

## Inställningar (Configuration)

Effektregler, elmätare, elområde och MQTT sätts här — inte i dashboarden.
Rumsprioritet kan justeras i panelen.

| Val | Betyder |
| --- | --- |
| **Price area** | Ditt elområde, SE1–SE4 |
| **Contract settlement** | Hur spotavtalet avräknas: dygn, timme eller kvart |
| **Log level** | Höj till `debug` om något beter sig konstigt |
| **Minimera effekttoppar** | Om optimeraren ska hålla nere debiterbara toppar |
| **Antal toppar / pris / fönster** | Enligt ditt elnätsavtal |
| **Elmätare (entitets-id)** | T.ex. `sensor.p1_meter_power` |
| **MQTT-host / port / user / lösen** | Normalt `core-mosquitto`. Används om Supervisorn inte ger broker |
| **HA-token / HA-URL** | Bara om Loggen visar `SUPERVISOR_TOKEN length=0` |

Rum, värmepump (`heat_pump`) och varmvatten (`hot_water`, inkl.
`setpoint_entity` för tankbörvärde) läggs i `/homeassistant/hemopt.yaml`
(bredvid `configuration.yaml`). Börja från `config.exempel.yaml` i repot.
Månader med effektavgift styrs också där under `peak_tariff.window.months`.

Efter ändring i Configuration: **Save**, sedan **Restart** (eller Rebuild).

## Home Assistant-API (viktigt)

Tillägget ska få `SUPERVISOR_TOKEN` automatiskt (`homeassistant_api: true`).
Om Loggen visar **`SUPERVISOR_TOKEN length=0`** och **HTTP 401**:

1. **Snabbast:** Skapa en *Long-lived access token* under din HA-profil →
   Säkerhet, klistra in under **Configuration → HA-token**, låt HA-URL vara
   `http://homeassistant:8123`, **Save**, **Restart**.
2. **Alternativ:** Avinstallera tillägget och installera om från GitHub-repot
   (data i `/data` behålls om du inte tar bort den), så Supervisorn ger en
   riktig token.

Utan token blir HA, väder och MQTT röda och ingen historik laddas — det hjälper
inte att vänta.

## Elmätare

Ange under **Configuration → Elmätare**. HomeWizard P1 heter oftast
`sensor.p1_meter_active_power`. Panelen visar bara vilken entitet som gäller.

## Elpris i panelen

Panelen visar **aktuellt spotpris** och en graf för **igår / idag / imorgon**.
Spotpriset hämtas från elprisetjustnu.se och kräver inte Home Assistant.

## Effekttoppar och elavtal

**Effektregler** i panelen är skrivskyddad status. Ändra under Configuration.
**Effekttoppar denna månad** visar tröskel, pågående timme och historik.
**Besparingsåtgärder** föreslår byte av avräkning och eventuell sänkning av
huvudsäkring (25 → 20 → 16 A) när det finns tillräckligt med mätdata — annars
står det att mer data behövs, och om du redan ligger rätt syns det tydligt.
**Användning och besparing** ritars när elmätaren ger data. **Jämför elavtal**
visar varje avräkningsform (månad / dygn / timme / kvart) både **utan** och
**med** lastflytt, och hur mycket det skiljer mot ditt nuvarande avtal
(behöver minst ett par dygns mätdata).

## Innan du litar på styrningen

Tillägget styr ingenting förrän du slår på **Styr värmen** i panelen. Låt det
gå några dygn först. Rummens tröghet lärs in från historik (ungefär en vecka)
**även med styrningen av** — träningen läser Home Assistants recorder, inte
aktuella börvärdesskrivningar. Slå på styrningen först när Log/doctor visar
att climate-entiteterna finns och planen ser vettig ut.

Önskad rumstemperatur ställer du under **Rum** i panelen («Önskad temp»).
Planen reglerar kring det bandet; prio 1 hålls hårdast.

## Felsök

Det finns **ingen** Info-toggle «Allow Home Assistant API». Tillägget har
`homeassistant_api: true` i manifestet, så Supervisorn ska ge
`SUPERVISOR_TOKEN` automatiskt.

| Symptom | Att göra |
| --- | --- |
| Home Assistant / väder / MQTT röda | Öppna **Log**. Om `SUPERVISOR_TOKEN length=0`: sätt **HA-token** under Configuration (se ovan) eller installera om tillägget. Behöver `HA API ping HTTP 200`. |
| Token length 0 | Supervisorn gav ingen token. Använd long-lived token-fallback eller reinstallera. |
| Minimera toppar av i Configuration men På i panelen | Bug i äldre version: `false` ignorerades. Uppdatera till **0.1.6+**, spara om Configuration, **Restart**. |
| MQTT «Not authorized» / röd | Sätt **MQTT-host** `core-mosquitto` + user/lösen under Configuration (0.1.12+). Från 0.1.8 stängs yaml-MQTT av i add-on tills broker finns. |
| Ingen elmätare | Sätt entitets-id under Configuration. |
| Ingen plan / inga rum | Skapa `/homeassistant/hemopt.yaml` från exemplet. |
| Spotpris saknas | Nätverk utåt till elprisetjustnu.se; kolla Log. |

Loggen ligger på tilläggets **Log**-flik. Entitets-ID:n under
**Developer tools → States**.
