# Slingkoll

Kontrollerar att varje termostat styr golvvärmeslingan i sitt eget rum. Om
någon inte gör det visar Slingkoll vilket rum den faktiskt värmer och hur du
kopplar om.

Behövs när rumsgivarna har parats ihop med kanalerna på golvvärmecentralen
utan att man visste vilken slinga som går till vilket rum.

## Installera

1. **Settings → Add-ons → Add-on Store**
2. Har du inte lagt till repot än: trepunktsmenyn → **Repositories**, klistra
   in `https://github.com/danebananee/hemopt`, **Add**, **Close**, och ladda
   om sidan.
3. Välj **Slingkoll** → **Install**.
4. Fliken **Info**: slå på **Show in sidebar** och tryck **Start**.
5. Öppna **Slingkoll** i vänstermenyn.

Inget behöver ställas in under **Configuration**.

## Så här går det till

**1. Termostater och givare.** Slingkoll har redan valt alla termostater den
hittat (för LK Arc är det `climate.*_thermostat`). Tryck **Ändra** om något ska
bort, eller om namnen inte stämmer med rummen termostaterna sitter i. Har du
temperaturgivare i rum utan egen termostat, till exempel hall eller badrum,
kryssa i dem också. Då syns det om en slinga egentligen värmer ett sådant rum.

**2. Snabbkoll i historiken (valfritt).** Letar på några sekunder efter
typiska tecken i det Home Assistant sparat senaste veckan. En termostat vars
slinga ligger i ett annat rum begär värme nästan jämt men blir aldrig varm,
och rummet som får värmen är varmt fast dess termostat aldrig begär någon.
Det är en fingervisning, inget säkert besked.

**3. Testa slingorna.** Tryck **Starta testet**. Termostaterna ställs
omväxlande på 28 °C, så att slingan öppnar, och 10 °C, så att den stänger, i
ett mönster där hälften av slingorna är på åt gången. Varje fas är tre timmar
(två om huset bara har trägolv). Slingkoll ser vilka rum som blir varmare när
en viss termostat är på, och räknar ut vilken slinga som hör till vilket rum.
Testet tar ungefär ett och ett halvt dygn, och förlängs om svaret inte är
säkert.

Under testet:

- hålls rummen mellan 18 och 25 °C (går att ändra). Blir ett rum kallare
  eller varmare pausas testet och termostaterna går som vanligt tills rummet
  har hämtat sig;
- höjs värmepumpens rumsbörvärde lite om vattnet till golvet är för svalt;
- är hemopts styrning avstängd, om du har hemopt.

När testet är klart, när du avbryter, eller om tillägget stängs av, får alla
termostater, värmepumpen och hemopt tillbaka sina gamla inställningar.

Bäst resultat blir det när det är kallt ute och värmen går. Elda inte i
kaminen och låt dörrarna vara som vanligt under testet.

## Resultatet

Varje termostat får ett av dessa besked:

- **✓ Styr sitt eget rum**: rätt kopplad.
- **! Styr slingan i …**: fel kopplad. Rummet som nämns är det slingan
  faktiskt ligger i.
- **? Troligen …**: inte säkert än.
- **– Ingen tydlig effekt**: inget av de mätta rummen blir varmare. Slingan
  ligger troligen i ett rum utan givare, eller så är kanalen inte kopplad till
  något ställdon.

Under **Så rättar du** står vad som ska ändras, till exempel *Byt kanal mellan
termostaterna i Kök och Tvättstuga*. Ändra kopplingen i LK:s app (vilken kanal
på centralen som hör till vilken rumsgivare), eller flytta ställdonen mellan
slingorna på fördelaren. Kör sedan testet en gång till för att kontrollera.

**Hur säkert är det?** visar hela tabellen: hur många grader i timmen varje
termostats slinga ger varje rum.

## Felsök

| Symptom | Att göra |
| --- | --- |
| Inga termostater hittas | Kontrollera att termostaterna syns som `climate.*` under **Developer tools → States**. För LK Arc behövs integrationen LK Arc Climate. |
| «Värmepumpen skickar inget varmt vatten» | Värmen är avstängd (sommarläge) eller det är för varmt ute. Kör testet en kallare dag. |
| Testet pausas hela tiden | Ett rum klarar inte gränserna. Sänk lägsta temperaturen under **Ändra**, eller kör testet när det är kallare ute. |
| «… ändras tillbaka av något annat» | En automation, LK-appen eller hemopt ställer om termostaten. Stäng av den under testet. |
| Panelen visar gamla uppgifter | Ladda om sidan (**Ctrl+Shift+R**). |
