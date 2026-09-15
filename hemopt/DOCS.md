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
3. Klistra in `https://github.com/DITT-GITHUB-NAMN/hemopt`, **Add**, **Close**
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
6. Fliken **Configuration**: välj **Price area** och **Contract settlement**,
   tryck **Save**
7. Fliken **Info**: slå på **Start on boot**, **Watchdog** och
   **Show in sidebar**, tryck **Start**
8. Öppna **Kostnadsoptimering** i vänstermenyn

Ingen token att skapa, inget MQTT-lösenord att skriva in, ingen YAML att
redigera.

## Vad tillägget redan vet

| Sak | Varifrån |
| --- | --- |
| Home Assistant-API | `SUPERVISOR_TOKEN`, ges av Supervisorn |
| MQTT-broker | Supervisorns tjänste-API, om Mosquitto är installerat |
| Lagring | `/data`, överlever uppdateringar |

Därför finns varken token eller lösenord bland inställningarna.

## Inställningar

Allt annat — rum, givare, komfortgränser, prioriteter, regler för effekttoppar
— ställs in i tilläggets egen panel, inte under **Configuration**. Profilen
sparas som data i `/data/profile.json`, och det är det som gör att samma
tillägg kan installeras oförändrat i ett annat hushåll.

| Val | Betyder |
| --- | --- |
| **Price area** | Ditt elområde, SE1–SE4 |
| **Contract settlement** | Hur spotavtalet avräknas: dygn, timme eller kvart |
| **Log level** | Höj till `debug` om något beter sig konstigt |

## Innan du litar på styrningen

Tillägget styr ingenting förrän du slår på **Styr värmen** i panelen. Låt det
gå några dygn först och jämför den planerade kurvan mot verkligheten.
Modellerna behöver historik, och tröghet per rum tar ungefär en vecka att lära
in.

## Felsök

Panelens **Diagnos**-vy kontrollerar varje entitet tillägget är konfigurerat
att använda och föreslår rättningar för dem som inte finns. Börja alltid där.

Loggen ligger på tilläggets **Log**-flik. Systemloggen finns under
**Settings → System → Logs**.

Entitets-ID:n slår du upp under **Developer tools → States**.
