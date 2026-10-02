# Ändringar

## 0.1.5

- Våningar: varje termostat har en våning, och en slinga räknas bara mot rum
  på samma våning. Golvtyp per våning.
- Testa en våning i taget (övriga termostater går som vanligt): färre
  slingor per test ger snabbare och säkrare svar.
- Förinställt: sju termostater på övervåningen (trägolv, 2 h per fas) och
  Salong, Lekrum, Entré och Tvättstuga på bottenvåningen (betong, 3 h).

## 0.1.4

- Starta/avsluta frågar på sidan i stället för i en ruta som Home
  Assistant-appen blockerar. Starten svarar direkt.

## 0.1.3

- Rättar «401: Unauthorized»: nyckeln till Home Assistant lästes inte.
- Förinställd med samma elva rum och värmepumpsentiteter som hemopt.

## 0.1.2

- Rättar «No module named slingkoll» vid start.

## 0.1.1

- Panelen startar direkt i stället för att vänta på Home Assistant (gav
  «502: Bad Gateway» under uppstarten). Fel vid start visas i panelen.

## 0.1.0

- Första versionen: snabbkoll i historiken, test av alla slingor med
  komfortskydd, automatisk höjning av värmepumpen, resultat med instruktioner
  för omkoppling.
