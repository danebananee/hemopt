# Kostnadsoptimering (hemopt)

Värmer huset och varmvattnet när elen är billig, laddar elbil och husbatteri
på rätt timmar, håller nere effekttopparna och räknar ut vad du sparar. Du
ställer in allt med klick i panelen; ingen YAML behövs.

Menyvägarna nedan står på engelska, eftersom Home Assistant oftast är det.

## Installera

1. **Settings → Add-ons → Add-on Store**
2. Trepunktsmenyn uppe till höger → **Repositories**
3. Klistra in `https://github.com/danebananee/hemopt`, **Add**, **Close**
4. Ladda om sidan och välj **Kostnadsoptimering (hemopt)** → **Install**.
   Första installationen tar några minuter på en Raspberry Pi.
5. Fliken **Info**: slå på **Start on boot**, **Watchdog** och
   **Show in sidebar**, tryck **Start**.
6. Öppna **Kostnadsoptimering** i vänstermenyn.

Har du **Mosquitto broker** installerad (Add-on Store → Mosquitto broker) dyker
hemopts sensorer och knappar upp i Home Assistant av sig själva. Det behövs
inte för att panelen ska fungera.

## Kom igång

Första gången visar panelen **Välkommen till hemopt** och en knapp,
**Kom igång**. Den öppnar fliken **Inställningar**, där hemopt redan har letat
igenom Home Assistant och fyllt i det den hittat:

- **Grundläggande**: elområde, elavtal och huvudsäkring (står på fakturorna).
- **Rum**: ett rum per termostat, med temperaturgivaren som hör till. Tryck
  **Hämta termostater från Home Assistant** om listan är tom. Ge rummen bra
  namn och välj prioritet: *Håll* rubbas aldrig, *Flexibel* får svaja mest.
- **Värmepump och varmvatten**: utetemperatur behövs, resten är valfritt.
- **Elmätare och väder**: en mätare på elmätarens P1-port (HomeWizard,
  Tibber Pulse …) ger förbrukning, effekttoppar och säkringskoll. Väder ger
  bättre förvärmning och räknar med solen.
- **Husbatteri** och **Elbil**: kryssa i om du har, se nedan.
- **Braskamin**: bara namnet; resten lär sig hemopt.

Tryck **Spara inställningarna**. hemopt börjar planera direkt. Listan överst
visar vad som är klart och vad som saknas.

Allt kan ändras senare under **Inställningar**. Det du sparar där gäller före
tilläggets **Configuration**-flik.

## Styr eller bara titta

hemopt styr ingenting förrän du slår på **Styr värmen** högst upp i panelen.
Låt den gå en vecka eller två först. Under tiden:

- lär sig modellen hur varje rum reagerar på värme, sol, blåst och brasa,
- räknar fliken **Besparing** ut vad du hade sparat med kvartspris och styrning
  jämfört med ditt nuvarande avtal,
- kan du jämföra planen med hur huset faktiskt beter sig.

När du slår på styrningen skriver hemopt börvärden till termostaterna, till
varmvattnet, till batteriet och till laddboxen.

## Husbatteri

hemopt laddar batteriet när elen är billig och låter huset använda det när elen
är dyr eller när en effekttopp hotar. Det säljer aldrig el till nätet.

Batteriet styrs via **en** entitet med ett effektbörvärde: plus laddar, minus
laddar ur, i watt eller kilowatt. Många batteriintegrationer har en sådan,
annars går det att göra med en hjälpare och en automation. Anger du också
batteriets **laddnivå** blir planen exakt; utan den antas halvfullt.

## Elbil och V2H

Ange avgångstid och hur full bilen ska vara. hemopt laddar på de billigaste
timmarna fram till dess och ser till att bilen är klar i tid.

- **Laddboxens styrning** är helst laddboxens strömgräns i ampere (Easee,
  Zaptec, Wallbox …). En vanlig på/av-knapp fungerar också.
- **Kabel i** gör att hemopt vet när bilen står hemma. Utan den antas bilen
  stå hemma från 17 till avgångstiden.
- **Bilens laddnivå** från bilens integration gör planen exakt.

**V2H** (bilen driver huset) kräver en dubbelriktad laddbox. Kryssa i
*Bilen får driva huset* och ange hur mycket laddning som alltid ska lämnas
kvar.

## Huvudsäkring

Under **Besparing → Huvudsäkring** bedömer hemopt om säkringen har rätt storlek
utifrån strömmen per fas som elmätaren mäter: om du skulle klara dig med en
mindre (billigare abonnemang), om marginalen är liten, eller om säkringen är
för liten. Tillfällen när strömmen var nära gränsen listas med datum och fas.

Rådet blir säkrare med tiden. Det kräver minst två veckors mätning och är
bäst när en kall period har passerat. Byte av säkring beställs hos nätbolaget.

## Braskamin

Tryck **Jag har tänt brasan** under **Rum och värme** när du eldar (eller slå
på `switch.hemopt_wood_stove_lit` i Home Assistant) och **Brasan har slocknat**
när den brunnit ut. Efter några kvällar vet hemopt hur mycket brasan värmer
varje rum, känner själv igen en tänd brasa och tipsar om när det lönar sig
att elda. Har du en givare vid kaminen kan den anges i Inställningar.

## Sensorer i Home Assistant

Med MQTT får du bland annat:

| Entitet | Vad |
| --- | --- |
| `sensor.hemopt_saving_today` / `_month` / `_per_day` | Vad kvartspris och styrning hade sparat |
| `sensor.hemopt_model_action` | Vad hemopt gör just nu, i klartext |
| `switch.hemopt_control_enabled` | Styr värmen på/av |
| `switch.hemopt_wood_stove_lit` | Markera att brasan brinner |
| `sensor.hemopt_battery_planned` / `ev_planned` | Planerad effekt för batteri och bil |
| `number.hemopt_priority_<rum>` | Rummets prioritet 1–3 |

## Felsök

| Symptom | Att göra |
| --- | --- |
| Panelen fastnar på «Läser in…» | Ladda om sidan med **Ctrl+Shift+R** (mobil: dra ned). |
| Home Assistant, väder eller MQTT är röda under **System** | Öppna tilläggets **Log**. Står det `SUPERVISOR_TOKEN length=0`, se nedan. |
| Inga rum hittas | Kontrollera att termostaterna syns under **Settings → Devices & services**. Rum kan också läggas till för hand med **Lägg till rum**. |
| Ingen elmätare | Välj den under **Inställningar → Elmätare och väder**. |
| Spotpris saknas | Tillägget behöver nå elprisetjustnu.se; se **Log**. |

**Om Loggen visar `SUPERVISOR_TOKEN length=0`:** skapa en *Long-lived access
token* under din profil i Home Assistant (längst ned under *Security*), klistra
in den under tilläggets **Configuration → HA-token**, **Save**, **Restart**.

## För den som vill veta mer

- Inställningarna sparas i tilläggets `/data/profile.json`. En handskriven
  `/homeassistant/hemopt.yaml` används om den är nyare. Mallen finns som
  `config.exempel.yaml` i repot.
- Har huset LK Systems golvvärme installerar tillägget LK Arc Climate så att
  varje rum får en termostat. Det görs bara om LK Systems-integrationen finns.
- Hur modellen och besparingskalkylen fungerar beskrivs i `README.md`.
- `GET /api/loop-mapping` är en tillfällig diagnostik för golvvärmeslingor
  som misstänks vara kopplade till fel termostat. Den rör aldrig styrningen.
