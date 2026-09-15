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
6. Fliken **Configuration**: välj elområde, avräkning och effektregler, tryck
   **Save**
7. Fliken **Info**: slå på **Start on boot**, **Watchdog**,
   **Show in sidebar** och **Home Assistant API**, tryck **Start**
   (eller **Rebuild** om API-åtkomst nyss slogs på)
8. Öppna **Kostnadsoptimering** i vänstermenyn

Ingen token att skapa, inget MQTT-lösenord att skriva in.

## Vad tillägget redan vet

| Sak | Varifrån |
| --- | --- |
| Home Assistant-API | `SUPERVISOR_TOKEN`, ges av Supervisorn |
| MQTT-broker | Supervisorns tjänste-API, om Mosquitto är installerat |
| Lagring | `/data`, överlever uppdateringar |

## Inställningar (Configuration)

Effektregler och elmätare sätts här — inte i dashboarden.

| Val | Betyder |
| --- | --- |
| **Price area** | Ditt elområde, SE1–SE4 |
| **Contract settlement** | Hur spotavtalet avräknas: dygn, timme eller kvart |
| **Log level** | Höj till `debug` om något beter sig konstigt |
| **Minimera effekttoppar** | Om optimeraren ska hålla nere debiterbara toppar |
| **Antal toppar / pris / fönster** | Enligt ditt elnätsavtal |
| **Elmätare (entitets-id)** | T.ex. `sensor.p1_meter_active_power` |

Rum, värmepump och övrig husbeskrivning läggs i
`/homeassistant/hemopt.yaml` (bredvid `configuration.yaml`). Börja från
`config.exempel.yaml` i repot. Månader med effektavgift styrs också där under
`peak_tariff.window.months`.

## Elmätare

För att se och kapa **effekttoppar** behövs husets totala effekt. Ange den under
**Configuration**, eller skriv entitets-id i panelen (fungerar även när listan
är tom / HA tillfälligt offline). HomeWizard P1 heter oftast
`sensor.p1_meter_active_power`.

## Elpris i panelen

Panelen visar **aktuellt spotpris** och en graf för **igår / idag / imorgon**.
Spotpriset hämtas från elprisetjustnu.se och kräver inte Home Assistant.

## Effekttoppar och elavtal

**Effektregler** i panelen är skrivskyddad status. Ändra under Configuration.
**Effekttoppar denna månad** visar tröskel, pågående timme och historik.
**Användning och besparing** ritars när elmätaren ger data. **Jämför elavtal**
behöver minst ett par dygns mätdata.

## Innan du litar på styrningen

Tillägget styr ingenting förrän du slår på **Styr värmen** i panelen. Låt det
gå några dygn först. Rummens tröghet lärs in från historik (ungefär en vecka).
Du behöver normalt sett inte skriva om koden för att styrningen ska bli bättre
— modellerna tränas om när det finns data.

## Felsök

| Symptom | Att göra |
| --- | --- |
| Home Assistant röd / «unreachable» | Info → tillåt **Home Assistant API** → **Rebuild**. Kolla Log efter `HA Core proxy HTTP 200`. |
| Ingen elmätare i listan | Skriv entitets-id manuellt eller i Configuration. |
| Ingen plan / inga rum | Skapa `/homeassistant/hemopt.yaml` från exemplet. |
| Spotpris saknas | Nätverk utåt till elprisetjustnu.se; kolla Log. |

Loggen ligger på tilläggets **Log**-flik. Entitets-ID:n under
**Developer tools → States**.
