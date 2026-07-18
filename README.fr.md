<div align="center">

# metmux

**Éditeur de métadonnées interactif, multi-format, en ligne de commande, avec intégration clic droit.**

**Statut : stable (v1.0.0).** Validé par un banc de 525 tests, fuzzing property-based compris.

**100% local : aucun réseau, aucune télémétrie.**

**Compatible avec : Windows, macOS, Linux.**

[Pourquoi](#pourquoi) · [Installation](#installation) · [Utilisation](#utilisation) · [Configuration](#configuration-configjson) · [Formats gérés](#formats-gérés) · [Architecture](#architecture) · [Développement](#développement) · [Roadmap](#roadmap) · [Crédits](#crédits) · [Licence](#licence)

</div>

metmux est un programme Python qui permet de modifier les métadonnées d’une grande variété de fichiers via une TUI (interface interactive dans le terminal).

Son nom est la contraction de *metadata multiplexer* : *met* pour metadata, son cœur de métier ; *mux* pour multiplexeur, le système qui choisit le bon moteur spécialisé (exiftool, ffmpeg, mutagen…) selon le type de fichier.

Son objectif est la praticité : il se lance d’un simple clic droit sur n’importe quel(s) fichier(s) ou dossier(s). C’est un seul outil là où il en faudrait normalement plusieurs, dans un langage littéral et rapide d’usage.

<div align="center">
  <img src="assets/readme_demo.gif" alt="Démonstration de metmux" width="800">
</div>

---

## Pourquoi

En voulant mettre de l’ordre dans mes archives, j’ai commencé à chercher des logiciels open-source pour mes besoins spécifiques. Pour les doublons, j’ai trouvé l’excellent Czkawka/Krokiet. Pour les backups, FreeFileSync (ou rsync). Parfait.

Mais des fichiers bien rangés, c’est une chose, bien renseignés, c’est mieux. J’avais des métadonnées erronées ou absentes dans pas mal de cas (photos, musique, documents…). C’est là que j’ai cherché des logiciels de modification de métadonnées, et je me suis retrouvé avec plusieurs outils, certains avec interface, d’autres sans.

J’ai écrit des scripts pour automatiser certaines actions, ce qui me prenait du temps, et me laissait quand même frustré de la solution finale. Je ne comprenais pas qu’il n’existe pas un logiciel qui permette de faire tout ça à la fois.

L’existant (les moteurs) étant là, j’ai fini par créer une interface pour dialoguer avec lui : c’est de là que metmux est né.

Plus d’encombrement, tout à portée de main, en un clic droit. C’était exactement ce que je voulais depuis le départ, et je me dis que je ne suis peut-être pas le seul.

J’ai donc étendu les fonctionnalités au-delà de l’usage initial pour en faire un produit utile à ceux que ça intéresse.

### L’existant, et ses limites

Pour situer ce que metmux remplace, voici ce qui existait déjà, deux familles d’outils, chacune avec ses contraintes.

**(a) Interface visuelle :** un logiciel par famille, à empiler.


<table>
  <thead>
    <tr>
      <th align="center">Famille</th>
      <th align="center">Propriétaire</th>
      <th align="center">Libre</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td align="center"><strong>Photos / Images</strong></td>
      <td>Lightroom (~12 $/mois), Bridge, Photo Mechanic (~149 $/an)</td>
      <td>digiKam, ExifToolGUI (Windows only), jExifToolGUI (arrêté 2023)</td>
    </tr>
    <tr>
      <td align="center"><strong>PDF</strong></td>
      <td>Acrobat Pro (~20 $/mois)</td>
      <td>—</td>
    </tr>
    <tr>
      <td align="center"><strong>Audio</strong></td>
      <td>Mp3tag (payant sur Mac), dBpoweramp (~48 $)</td>
      <td>Kid3, MusicBrainz Picard</td>
    </tr>
    <tr>
      <td align="center"><strong>Vidéo</strong></td>
      <td>MetaX (Windows, ~20 $)</td>
      <td>MKVToolNix (MKV only), Subler (MP4 + macOS only)</td>
    </tr>
    <tr>
      <td align="center"><strong>Bureautique</strong></td>
      <td>Microsoft Office</td>
      <td>LibreOffice</td>
    </tr>
    <tr>
      <td align="center"><strong>EPUB</strong></td>
      <td>—</td>
      <td>Calibre, Sigil</td>
    </tr>
    <tr>
      <td align="center"><strong>BD CBZ</strong></td>
      <td>—</td>
      <td>ComicTagger</td>
    </tr>
    <tr>
      <td align="center"><strong>Partitions MusicXML</strong></td>
      <td>—</td>
      <td>MuseScore</td>
    </tr>
    <tr>
      <td align="center"><strong>Géo (KMZ, GeoJSON)</strong></td>
      <td>Google Earth</td>
      <td>QGIS</td>
    </tr>
    <tr>
      <td>ipynb, plist, eml, mbox, har, sqlite, m3u, cue, tcx, webloc…</td>
      <td colspan="2"><em>Aucun éditeur grand public : édition à la main ou outils développeur</em></td>
    </tr>
  </tbody>
</table>


> Contraintes : plusieurs logiciels à installer, maîtriser et entretenir, fragmentés par formats et OS. Et pour toute une série de formats, aucune couverture. Ces logiciels offrent souvent bien plus que la modification de métadonnées, ce qui les rend lourds pour ce seul besoin.

**(b) Outils en ligne de commande :** une syntaxe distincte pour chacun.

Exemples pour modifier un titre :

- **exiftool** : `exiftool -Title="Mon titre" photo.jpg`
- **ffmpeg** : `ffmpeg -i in.mp4 -metadata title="Mon titre" -c copy out.mp4`
- **mutagen** : `audio = EasyID3("chanson.mp3"); audio["title"] = "Mon titre"; audio.save()`

> Contraintes : maîtriser la syntaxe, et savoir lequel appeler selon l’extension.

### Ce qu’apporte metmux

metmux :
1. Propose une interface visuelle sans GUI.
2. Se lance d’un clic droit sur n’importe quel(s) fichier(s) ou dossier(s) depuis le gestionnaire de fichiers.
3. Adopte un langage unifié, littéral et rapide.
4. Combine les différents moteurs de manière invisible selon l’extension (photos, musique, vidéo, documents, EPUB…).
5. Modifie les métadonnées internes et externes à la fois.
6. Offre des options supplémentaires : effacement total, édition par lot, décalage de dates…

Le tout cross-platform (Windows, macOS, Linux) et libre : un point d’entrée unique qui rend tous ces moteurs accessibles à tous.

---

## Installation

### Ce qu’il vous faut

<table>
  <thead>
    <tr>
      <th align="center">Outil</th>
      <th align="center">Pourquoi</th>
      <th align="center">Obligatoire ?</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>Python 3.8+</strong></td>
      <td>fait tourner le programme</td>
      <td align="center">✓ oui</td>
    </tr>
    <tr>
      <td><strong>exiftool</strong></td>
      <td>images, PDF, et dates de tous les fichiers</td>
      <td align="center">✓ oui</td>
    </tr>
    <tr>
      <td><strong>ffmpeg / ffprobe</strong></td>
      <td>conteneurs vidéo (MKV, AVI, WebM…)</td>
      <td align="center">⟡ recommandé</td>
    </tr>
    <tr>
      <td><strong>mutagen</strong> (module Python)</td>
      <td>fichiers audio (MP3, FLAC…)</td>
      <td align="center">⟡ recommandé</td>
    </tr>
  </tbody>
</table>

Les outils en « recommandé » débloquent les formats vidéo et audio. Les formats pris en charge par la stdlib Python (Office, EPUB, eml, sqlite, ipynb…) fonctionnent sans eux.

### Installer les outils

**Deux familles d’outils, installations différentes :**

- **Programmes autonomes** : `exiftool` et `ffmpeg`.
- **Module Python** : `mutagen`.

Le plus simple sur les trois systèmes : un gestionnaire de paquets (winget sur Windows, Homebrew sur macOS, apt sur Linux). Il télécharge le programme, le range au bon endroit et règle le PATH à votre place.

**Windows** — avec winget, le gestionnaire de paquets intégré à Windows 10 et 11. Windows n’embarque pas Python : la première ligne l’installe (sautez-la si vous l’avez déjà) :
```sh
winget install -e --id Python.Python.3.12
winget install -e --id OliverBetz.ExifTool
winget install -e --id Gyan.FFmpeg
pip3 install mutagen
```
<details>
<summary>Sans winget — installation manuelle</summary>

1. **Python** : sur python.org, téléchargez l’installateur et lancez-le en cochant « Add python.exe to PATH ».
2. Créez `C:\Tools` ou tout autre dossier stable.
3. **ExifTool** : sur exiftool.org, téléchargez l’archive, décompressez-la, renommez `exiftool(-k).exe` en `exiftool.exe`, et placez-le dans `C:\Tools\exiftool`.
4. **ffmpeg** : sur ffmpeg.org, cliquez sur l’icône Windows pour télécharger l’archive, décompressez-la, copiez le sous-dossier `bin` dans `C:\Tools\ffmpeg`. Résultat : `C:\Tools\ffmpeg\bin\ffmpeg.exe` (avec `ffprobe.exe`). Le reste de l’archive est inutile.
5. Ajoutez ces dossiers au PATH : barre de recherche → « Modifier les variables d’environnement système » → *Variables d’environnement* → sélectionnez `Path` → *Modifier* → *Nouveau* → tapez `C:\Tools\exiftool`. Refaites *Nouveau* pour `C:\Tools\ffmpeg\bin`. Validez par *OK*.
6. **mutagen** : `pip3 install mutagen` dans la console.
---
</details>

**macOS** — avec [Homebrew](https://brew.sh) :
```sh
brew install exiftool ffmpeg
pip3 install mutagen
```

<details>
<summary>Sans Homebrew — installation manuelle ou MacPorts</summary>

1. **ExifTool** : sur exiftool.org, téléchargez le `.pkg` et double-cliquez. Il s’installe dans `/usr/local/bin`, déjà dans le PATH : rien d’autre à faire.
2. **ffmpeg** : sur ffmpeg.org, cliquez sur l’icône macOS pour télécharger l’archive, et décompressez-la : vous obtenez un fichier nommé `ffmpeg`. Dans le terminal, tapez `sudo mkdir -p /usr/local/bin && sudo mv ~/Downloads/ffmpeg /usr/local/bin/` puis votre mot de passe. Cela crée le dossier `/usr/local/bin`, seulement s’il n’existe pas déjà, et y déplace ffmpeg.
3. **mutagen** : `pip3 install mutagen` dans le terminal.

Alternative via **[MacPorts](https://www.macports.org)** (autre gestionnaire de paquets) : `sudo port install exiftool ffmpeg`, puis l’étape 3.

---
</details>

**Linux** — avec le gestionnaire de paquets de votre distribution.

**Debian / Ubuntu** :
```sh
sudo apt install libimage-exiftool-perl ffmpeg python3-mutagen
```

**Fedora** — le `ffmpeg` complet nécessite RPM Fusion (activé d’abord) :
```sh
sudo dnf install \
  https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm \
  https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install perl-Image-ExifTool ffmpeg python3-mutagen
```

**Arch** :
```sh
sudo pacman -S perl-image-exiftool ffmpeg python-mutagen
```

### Récupérer le programme

[`metmux.py`](metmux.py) est un fichier unique, mais le lancement au clic droit a aussi besoin des scripts du dossier [`integrations/`](integrations/). Le plus simple est donc de récupérer le dépôt entier.

- **Sans git** : bouton vert « Code » en haut de la page → « Download ZIP », puis décompressez l’archive.
- **Avec git**, même commande sur Windows, macOS et Linux :

```sh
git clone https://github.com/michaelbruzy/metmux
cd metmux
```

**Bravo, metmux est installé**. Reste une dernière étape : l’installation du clic droit (usage recommandé), expliquée pas à pas pour Windows, macOS et Linux dans [integrations/README.fr.md](integrations/README.fr.md).

<div align="center">
  <img src="assets/readme_integrations.png" alt="Intégration clic droit sur macOS, Windows et Linux" width="800">
</div>


> Usage terminal seul ? [`metmux.py`](metmux.py) suffit, ou installez la commande `metmux` avec pip. Tout est rassemblé dans [Au terminal](#ouvrir-metmux-au-terminal) plus bas.

---

## Utilisation

### Lancer metmux

Au clic droit, vous n’avez aucun réglage à indiquer : metmux s’adapte automatiquement à ce que vous avez sélectionné.

- **Un seul fichier** : vous éditez ce fichier.
- **Plusieurs fichiers** : metmux demande d’abord ce que vous voulez. `g` édite le lot d’un coup (mode groupe), `s` édite fichier par fichier (flèches ← / → du clavier ou `n`/`p` pour naviguer). Le choix n’est jamais définitif : `s` et `g` basculent de l’un à l’autre en session (voir [les commandes](#les-commandes-)).
- **Un dossier** : vous éditez les fichiers directement dedans, pas les sous-dossiers (traitement non-récursif).

### Les trois vues et le code visuel

metmux démarre sur la vue `edit`.
> Les formats en lecture seule intégrale (mbox, tcx, paquets applicatifs) s’ouvrent sur `all`, rien n’y étant modifiable.

On change de vue en tapant son nom :

<table>
  <thead>
    <tr>
      <th align="center">Vue</th>
      <th align="center">Ce qu’elle montre</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td align="center"><kbd>all</kbd></td>
      <td>tous les champs</td>
    </tr>
    <tr>
      <td align="center"><kbd>in</kbd></td>
      <td>uniquement les champs avec une valeur</td>
    </tr>
    <tr>
      <td align="center"><kbd>edit</kbd></td>
      <td>uniquement les champs modifiables (avec ou sans valeur)</td>
    </tr>
  </tbody>
</table>

Le code visuel indique si les champs sont éditables ou non :

- **Gras blanc** = champ modifiable (peut être vide ou rempli).
- Gris moyen = champ en lecture seule / non modifiable.

### Pendant une session

Au prompt, vous tapez soit un champ à modifier, soit une commande. Les commandes et les noms de champs sont insensibles à la casse (`q`, `Q`, `artist`, `ARTIST`) ; seules les valeurs que vous écrivez gardent exactement la casse tapée.

#### Réécrire un champ :

Pour modifier un champ, il suffit de le réécrire.

<table>
  <thead>
    <tr><th colspan="2" align="center">Vous pouvez écrire...</th></tr>
  </thead>
  <tbody>
    <tr><td><code>Titre : Coucher de soleil</code></td><td>le libellé, avec deux-points</td></tr>
    <tr><td><code>Titre:Coucher de soleil</code></td><td>deux-points collé (les espaces autour sont libres)</td></tr>
    <tr><td><code>Title : Coucher de soleil</code></td><td>le nom interne</td></tr>
    <tr><td><code>Titre Coucher de soleil</code></td><td>sans deux-points (un espace suffit)</td></tr>
    <tr><td><code>titre : Coucher de soleil</code></td><td>sans majuscule</td></tr>
    <tr><td><code>t : Coucher de soleil</code></td><td>l’alias (celui affiché entre parenthèses)</td></tr>
    <tr><td><code>t Coucher de soleil</code></td><td>l’alias sans deux-points (<strong>le plus rapide ←</strong>)</td></tr>
  </tbody>
</table>

> Il est également possible de renommer le fichier via le champ « nom du fichier ». metmux refuse les caractères `/`, `\`, `%`, les caractères de contrôle, un nom vide, un nom commençant par un point (fichier caché), un nom réservé par l’OS (`CON`, `NUL`…) ou un nom déjà pris.

#### Effacer un champ :

Il suffit d’écrire le champ seul, suivi d’un deux-points ou juste d’un espace, avec les mêmes combinaisons que ci-dessus. L’idée est qu’un champ vide est un champ effacé.
Exemples : `Titre :`, `t `, etc.

#### Coller une valeur :

Taper d’abord le nom du champ, coller, puis entrée.
Pour protéger du collage accidentel, coller sur une ligne vide est refusé, et le premier mot d’un texte collé n’est jamais lu comme un nom de champ.

Exemples :
`champ : (coller "a long time ago...")` → Le champ prend la valeur du bloc collé.
`(coller "a long time ago...")` → Le collage est refusé, et le « a » n’est pas reconnu comme le `a` du champ `artiste`.

#### Effacer la ligne :

`Ctrl-U` efface d’un coup toute la saisie.

#### Ajouter sans écraser :

Les champs listes (Mots-clés, Sujet, Catégorie, etc.) acceptent un « + » devant la valeur, qui s’ajoute au lieu d’écraser :

`Mots-clés +vacances` ← ajoute « vacances » aux mots-clés déjà présents.

> **Bon à savoir pour tous les champs :**
> Un libellé en plusieurs mots passe. Exemple : `Artiste de l'album`.
> Une valeur (heure, URL, etc.) peut contenir « : ». Exemple : `titre : Trombone Concerto: III`

#### Les dates :

Les dates peuvent être modifiées séparément, ou toutes d’un coup via la commande `dates`.
Exemple : `dates 25/12/2024 14:00`.

Cette commande agit sur les horodatages du fichier, à l’exception de la date de création et de la date d’accès du système de fichiers, ainsi que des dates de l’*œuvre* (année d’un album, `originaldate`, date d’un film ou d’un livre, etc.). Les dates de l’œuvre restent modifiables individuellement, ainsi que la date de création (macOS et Windows uniquement). La date d’accès n’est jamais modifiable : le système la réécrit à chaque lecture.

##### Formats reconnus :

`2024` · `2024/12` · `12/2024` · `25/12/2024` · `2024/12/25` · `25/12/2024 14:00` · `25/12/2024 14:00:30` · `20241225` · `202412251400` · `20241225140000`

Le séparateur est libre : `/`, `-`, `.` ou `:` pour la date, et `:`, `h`, `m` ou `s` pour l’heure, sur tous les formats [vus plus haut](#formats-reconnus-).

Exemples : `25-12-2024` · `25/12/2024 14h00m30s`

##### Ordre jour/mois :

Par défaut, metmux lit *et affiche* les dates en format européen (« `eu` ») : `25/12/2024` = 25 décembre. Pour l’ordre américain, tapez `us` dans le programme (`12/25/2024`). Le choix est aussitôt enregistré dans `config.json` et conservé pour les sessions suivantes.

##### Décaler une date :

Il est aussi possible de décaler une date, ou toutes : un « + » ou un « - » suivi d’une durée en jours (d), heures (h), minutes (m) ou secondes (s).

Exemples :
- `dates +2h` ou `dates -1d` : décale tous les horodatages visés par `dates` (+2 heures, −1 jour).
- `FileModifyDate +1d2h` : décale cette seule date (+1 jour et 2 heures).

En lot, chaque fichier est décalé depuis sa propre valeur.

##### Règles à connaître :

Sur les horodatages du fichier, ce qui n’est pas précisé est complété au plus bas : 1ᵉʳ jour et 1ᵉʳ mois, heure à `00:00:00`.
Exemple : `dates 2024` appliquera la date `2024/01/01 00:00:00`.

Une année à 2 chiffres est lue 20xx : `25/12/24` = `25/12/2024`.

#### Édition en lot :

Sur un lot de fichiers, les valeurs communes sont fusionnées, et celles qui diffèrent d’un fichier à l’autre s’affichent comme `***`. Toute saisie s’applique alors à tous les fichiers à la fois. Seule exception, le renommage : indisponible en lot (tous les fichiers recevraient le même nom).

Exemple sur un album entier :
```
Artiste (a) : Sergueï Rachmaninov
Titre (t) : ***
```

#### Les commandes :

<table>
  <thead>
    <tr>
      <th align="center">Commande</th>
      <th align="center">Effet</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td align="center"><kbd>help</kbd> / <kbd>aide</kbd></td>
      <td>affiche l’aide</td>
    </tr>
    <tr>
      <td align="center"><kbd>all</kbd> / <kbd>in</kbd> / <kbd>edit</kbd></td>
      <td>change de vue (<a href="#les-trois-vues-et-le-code-visuel">voir plus haut</a>)</td>
    </tr>
    <tr>
      <td align="center"><kbd>s</kbd> / <kbd>single</kbd></td>
      <td>en mode groupe : reprend le lot fichier par fichier</td>
    </tr>
    <tr>
      <td align="center"><kbd>g</kbd> / <kbd>group</kbd></td>
      <td>en parcours fichier par fichier : revient à l’édition du lot entier d’un coup</td>
    </tr>
    <tr>
      <td align="center"><kbd>→</kbd> / <kbd>←</kbd></td>
      <td>parcours de lot : fichier suivant / précédent, sans Entrée, quand la ligne de saisie est vide</td>
    </tr>
    <tr>
      <td align="center"><kbd>n</kbd> / <kbd>p</kbd></td>
      <td>le même parcours, tapé comme une commande puis Entrée : fichier suivant (<code>n</code>, comme <em>next</em>) / précédent (<code>p</code>, comme <em>previous</em>)</td>
    </tr>
    <tr>
      <td align="center"><kbd>Ctrl-U</kbd></td>
      <td>efface d’un coup la ligne en cours de saisie</td>
    </tr>
    <tr>
      <td align="center"><kbd>fr</kbd> / <kbd>en</kbd></td>
      <td>change la langue de l’affichage (enregistré dans <code>config.json</code>)</td>
    </tr>
    <tr>
      <td align="center"><kbd>eu</kbd> / <kbd>us</kbd></td>
      <td>change l’ordre des dates ambiguës : <code>eu</code> = jour/mois, <code>us</code> = mois/jour (enregistré)</td>
    </tr>
    <tr>
      <td align="center"><kbd>dates …</kbd></td>
      <td>écrit ou décale les dates (voir ci-dessus)</td>
    </tr>
    <tr>
      <td align="center"><kbd>wipe</kbd></td>
      <td>efface toutes les métadonnées (annulable)</td>
    </tr>
    <tr>
      <td align="center"><kbd>u</kbd> / <kbd>undo</kbd></td>
      <td>annule la dernière modification</td>
    </tr>
    <tr>
      <td align="center"><kbd>ua</kbd> / <kbd>undo all</kbd></td>
      <td>annule toutes les modifications de la session</td>
    </tr>
    <tr>
      <td align="center"><kbd>q</kbd> / <kbd>quit</kbd> / <kbd>exit</kbd></td>
      <td>quitte</td>
    </tr>
  </tbody>
</table>

> Les commandes de lot (`s`, `g`, `→`/`←`, `n`/`p`) n’existent qu’à plusieurs fichiers.

> `wipe` demande une confirmation si plusieurs fichiers sont traités en même temps.

#### Annuler :

Toute modification s’applique aussitôt et reste annulable (`u`, `ua`) le temps de la session, y compris après être passé à un autre fichier du lot ou avoir basculé groupe/individuel. Une modification n’est plus annulable sitôt metmux fermé.

> ⚠ Exception pour l’annulation d’un `wipe` sur un fichier **audio, vidéo ou image** : l’annulation restaure les champs textuels, mais **pas** les éléments binaires embarqués (pochette, métadonnées par piste, chapitres ; vignette intégrée et données constructeur pour les images). metmux le signale au moment du `wipe`.
>
> Sur un **PDF**, `wipe` neutralise les métadonnées mais exiftool ne peut pas les retirer physiquement : elles restent techniquement récupérables dans le fichier. metmux le signale aussi au moment du `wipe`.

#### Focus :

Sur fichier seul. Taper le nom d’un champ seul (exemple : `paroles`), sans valeur ni espace final, affiche sa valeur complète, que la liste tronque. Si c’est une image embarquée (vignette, pochette d’album…), metmux l’ouvre dans votre visionneuse.

<details>
<summary><h3>Ouvrir metmux au terminal</h3></summary>

Le clic droit reste l’usage pour lequel `metmux` a été pensé, mais tout est aussi accessible en ligne de commande : ce que le clic droit choisit automatiquement selon la sélection y devient un réglage explicite, via `--mode`.

1. **Récupérer [`metmux.py`](metmux.py) seul** :
   - **Sans git** : téléchargez le seul fichier [`metmux.py`](metmux.py) via l’icône « Download raw file ».
   - **Avec git** : `git clone https://github.com/michaelbruzy/metmux` (récupère le dépôt entier ; seul `metmux.py` sert ici).
   - **Avec [pipx](https://pipx.pypa.io)** : `pipx install metmux` installe la commande `metmux` (mutagen compris) dans un environnement isolé, la voie recommandée pour un outil en ligne de commande. Sinon `pip3 install metmux` l’installe dans votre environnement Python courant.

2. **Lancer metmux** sur un ou plusieurs fichiers, en choisissant le mode :

```sh
python3 metmux.py --mode=MODE [fichier ou dossier ...]
```

Exemple :
```sh
python3 metmux.py --mode=single ma_photo.jpg
```

Installé via pip, remplacez `python3 metmux.py` par `metmux` : la commande est dans le PATH.

Quatre valeurs possibles pour `MODE` :
- `single` : ouvre une session fichier par fichier.
- `group` : ouvre une session unique qui édite tout le lot d’un coup.
- `ask` : à plusieurs fichiers, demande d’abord : `single` ou `group` (ce que les intégrations clic droit utilisent) ; à un seul fichier, ouvre directement la session normale.
- `wipe` : mode one-shot, vide tout le lot sans ouvrir de session ; le résultat s’affiche sur un écran récapitulatif, où l’annulation (`u` / `ua`) reste proposée.

3. **Aide et version** : `--version` (ou `-V`) affiche la version, `--help` (ou `-h`) rappelle la syntaxe.

> L’option `--gather` (utilisée par l’intégration Windows) fusionne les lancements quasi simultanés en une seule session : le menu contextuel Windows démarre un metmux par fichier sélectionné, et c’est ainsi qu’ils se retrouvent dans une seule fenêtre. Elle est inutile dans une session normale au terminal.

> Le collage protégé ([voir plus haut](#coller-une-valeur-)) a une seule exception : un terminal sans *bracketed paste* (console texte Linux brute, très vieux tmux/screen). Collez-y les valeurs ligne par ligne : sans cette fonction, un bloc multi-lignes collé pourrait exécuter sa première ligne comme une commande. Tous les terminaux modernes l’ont ; la console classique Windows ne l’a pas, mais metmux y lit lui-même le clavier et la protection tient sans elle.

</details>

---

## Configuration (`config.json`)

`config.json` garde les préférences d’une session à l’autre : la langue et le format de date.

Pas besoin d’y toucher ni de le créer : il s’écrit tout seul quand vous tapez `fr`/`en` ou `eu`/`us` dans le programme, et s’il est absent ou abîmé, metmux démarre sur ses réglages par défaut.

```json
{
  "lang": "en",
  "date_format": "eu"
}
```

- `lang` (`"en"` ou `"fr"`) : la langue au démarrage.
- `date_format` (`"eu"` ou `"us"`) : l’ordre jour/mois, appliqué à la fois à la lecture d’une date ambiguë que vous tapez et à l’affichage de toute date. `"eu"` place le jour d’abord (`25/12/2024`), `"us"` le mois d’abord (`12/25/2024`).
> Note : pour chaque réglage, la première valeur citée est celle par défaut.

---

## Formats gérés

<table>
  <thead>
    <tr>
      <th align="center">Catégorie</th>
      <th align="center">Formats</th>
      <th align="center">Moteur</th>
      <th align="center">Édition</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Images, PDF, divers</td>
      <td>jpg, jpeg, tiff, tif, png, gif, webp, heic, heif, cr2, cr3, nef, arw, dng, orf, rw2, raf, pdf (+ tout autre format reconnu par exiftool, en lecture seule quand exiftool ne sait pas l’écrire)</td>
      <td align="center">exiftool</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Audio</td>
      <td>mp3, flac, ogg, oga, opus, ape, wv, mpc, tta, ofr</td>
      <td align="center">mutagen</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Audio (tags en lecture seule)</td>
      <td>wma, aiff, aif</td>
      <td align="center">mutagen</td>
      <td>lecture seule (nom et dates du fichier restent modifiables)</td>
    </tr>
    <tr>
      <td>Vidéo (conteneurs)</td>
      <td>mkv, avi, flv, wmv, asf, webm, mka</td>
      <td align="center">ffmpeg</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Bureautique</td>
      <td>docx, xlsx, pptx, odt, ods, odp</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Livres / notebooks</td>
      <td>epub, ipynb</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Bande dessinée</td>
      <td>cbz</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Playlists</td>
      <td>m3u, m3u8</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Feuille d’index (cue sheet)</td>
      <td>cue</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Property lists</td>
      <td>plist, webloc, mobileconfig</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>E-mail</td>
      <td>eml</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Mailbox</td>
      <td>mbox</td>
      <td align="center">stdlib</td>
      <td>lecture seule</td>
    </tr>
    <tr>
      <td>Géo</td>
      <td>geojson, kmz</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Web</td>
      <td>har</td>
      <td align="center">stdlib</td>
      <td>lecture (+ commentaire)</td>
    </tr>
    <tr>
      <td>Bases</td>
      <td>sqlite, sqlite3, db</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture (app id, version)</td>
    </tr>
    <tr>
      <td>Partitions</td>
      <td>musicxml</td>
      <td align="center">stdlib</td>
      <td>lecture + écriture</td>
    </tr>
    <tr>
      <td>Activités sportives</td>
      <td>tcx</td>
      <td align="center">stdlib</td>
      <td>lecture seule</td>
    </tr>
    <tr>
      <td>Paquets applicatifs</td>
      <td>jar, war, ear, apk, xpi, ipa</td>
      <td align="center">stdlib</td>
      <td>lecture seule</td>
    </tr>
  </tbody>
</table>

> Pourquoi les paquets applicatifs sont en lecture seule : une appli (.apk Android, .ipa iOS, .jar Java…) embarque une signature numérique prouvant qu’elle n’a pas changé depuis sa fabrication. Le système la vérifie à l’installation. Modifier le fichier, même une métadonnée, casse cette signature : l’appli serait refusée comme corrompue. metmux les lit donc sans jamais y écrire.

Pour les formats absents de ce tableau, metmux modifie au moins les métadonnées externes : le nom et les dates tenues par le système de fichiers.

**Modifiables quel que soit le format et le système :**

<table>
  <thead>
    <tr>
      <th align="center">Champ</th>
      <th align="center">Ce que c’est</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>FileName</code> (Nom du fichier)</td>
      <td>le renommage (mêmes garde-fous : ni <code>/</code>, <code>\</code>, <code>%</code>, ni nom vide ou réservé)</td>
    </tr>
    <tr>
      <td><code>FileModifyDate</code> (Date de modification (fichier))</td>
      <td>l’horodatage <code>mtime</code> du système de fichiers</td>
    </tr>
  </tbody>
</table>

**Dépendantes du système d’exploitation :**

<table>
  <thead>
    <tr>
      <th align="center">Champ</th>
      <th align="center">Disponibilité</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Date de création</td>
      <td>Modifiable sur macOS et Windows, écrite par metmux sans outil externe. Sur Linux, aucune API en espace utilisateur ne permet d’écrire une date de création : elle y est en lecture seule. Affichée seulement là où le système de fichiers en conserve une (macOS et Windows ; Linux n’en expose pas).</td>
    </tr>
    <tr>
      <td>Date d’accès</td>
      <td>Lecture seule : toute lecture du fichier la met à jour, la réécrire serait sans effet.</td>
    </tr>
  </tbody>
</table>

Un type inconnu ou un fichier corrompu n’est jamais édité comme contenu : metmux le refuse (« Lecture du fichier impossible. ») ou retombe sur ses seules données externes (nom, dates du fichier).

### Ajouter un format

Un format vous manque ? Deux possibilités :

- **Le demander** : ouvrez une *issue* (un message public sur la page du dépôt, prévu pour signaler un besoin ou un bug) en décrivant le format, avec si possible un fichier d’exemple.
- **Le coder** : un format = un petit « moteur » à brancher dans le routage (voir [Architecture](#architecture)). On propose ensuite son ajout via une *pull request* (une proposition de modification du code).

---

## Architecture

`metmux` tient en **un seul fichier** ([`metmux.py`](metmux.py)), organisé en trois couches :

- **Routage par extension** (`engine_for`) : l’extension du fichier choisit le moteur ; tout ce qui n’a pas de moteur dédié part chez **exiftool**, qui couvre images, PDF et quantité d’autres types.
- **Une table de moteurs** (`ENGINES`) : chaque moteur expose le **même contrat de quatre fonctions** `read` / `write` / `writable` / `wipe`. Trois s’appuient sur un outil externe (exiftool, mutagen, ffmpeg) ; **dix-sept** n’utilisent que la **bibliothèque standard** de Python.
- **Logique commune**, partagée par tous les moteurs : lecture et écriture des dates, garde-fous sur les noms de champs, et le code visuel à l’écran.

Ajouter un format revient à écrire un tel moteur et à l’inscrire dans `ENGINES`, sans toucher au reste ; chaque moteur est éprouvé par la suite de tests (partie suivante).

**Une écriture ne modifie jamais le fichier sur place** : quel que soit le moteur, metmux écrit un fichier temporaire complet à côté de l’original, puis le renomme par-dessus. Une interruption ne laisse donc jamais un fichier à moitié écrit ; en contrepartie, chaque champ validé coûte une réécriture entière, et autant d’espace libre que la taille du fichier le temps de l’opération.

---

## Développement

Le banc fabrique ses propres fichiers de test : aucun fichier réel n’est touché. Les tests qui demandent un outil absent sont ignorés.

```sh
pip3 install pytest mutagen hypothesis
HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis python3 -B -m pytest tests/ -p no:cacheprovider
```

Vert intégral = le contrat encodé dans [`tests/metmux_test.py`](tests/metmux_test.py) est tenu. Toute fonctionnalité ajoutée doit d’abord recevoir ses propres assertions dans le banc.

Le banc inclut du **fuzzing property-based** (Hypothesis) : plutôt que des exemples figés, il génère des centaines d’entrées torturées (unicode, caractères de contrôle, valeurs vides ou démesurées) et vérifie qu’aucune fonction ne lève d’exception ni ne corrompt un fichier. Par défaut, la recherche rapide (parseurs, lecteurs, écritures stdlib) tourne à chaque exécution. Pour une campagne plus profonde, qui ajoute l’écriture end-to-end via exiftool/ffmpeg/mutagen, préfixez la même commande par `FUZZ=300` :

```sh
FUZZ=300 HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis python3 -B -m pytest tests/ -p no:cacheprovider
```

### Un dépôt qui reste propre

Tous les réglages tiennent dans la ligne, il n’existe aucun fichier de configuration à la racine. Chaque option neutralise un artefact, pour que la suite ne laisse **rien** derrière elle :

<table>
  <thead>
    <tr>
      <th align="center">Option</th>
      <th align="center">Effet</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>python3 -B</code></td>
      <td>pas de bytecode <code>__pycache__/</code> (<code>.pyc</code>)</td>
    </tr>
    <tr>
      <td><code>-p no:cacheprovider</code></td>
      <td>pas de cache pytest <code>.pytest_cache/</code></td>
    </tr>
    <tr>
      <td><code>HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis</code></td>
      <td>base d’exemples et caches d’Hypothesis (<code>.hypothesis/</code>) renvoyés hors du dépôt</td>
    </tr>
    <tr>
      <td><a href="tests/"><code>tests/</code></a></td>
      <td>collecte limitée au seul dossier des tests</td>
    </tr>
  </tbody>
</table>

Lancez toujours la suite avec cette commande complète : un simple `python3 -m pytest` réintroduirait ces dossiers à la racine.

---

## Roadmap

metmux v1.0.0 est stable et se suffit à lui-même. Les pistes ci-dessous sont envisagées pour la suite, sans engagement de date ni de périmètre :

- **Copier / coller de métadonnées :** mémoriser les champs d’un fichier et les appliquer à un autre, directement au clic droit.
- **Effacement ciblé « vie privée » :** retirer les seuls champs identifiants (GPS, auteur, logiciel, appareil…) en gardant titre et description.
- **Installeur multi-OS :** un script unique qui pose les intégrations clic droit au bon endroit, sans édition manuelle des chemins.
- **Écriture groupée :** appliquer plusieurs champs en une seule réécriture du fichier, plus rapide sur les très gros fichiers.

Une idée, un besoin ? Ouvrez une *issue* sur le dépôt.

---

## Crédits

`metmux` ne pourrait exister sans les outils sur lesquels il s’appuie :

- [**ExifTool**](https://exiftool.org) de Phil Harvey : licence Artistic / GPL ;
- [**FFmpeg**](https://ffmpeg.org) : licence LGPL (certaines options GPL) ;
- [**Mutagen**](https://github.com/quodlibet/mutagen) : licence GPL-2.0-or-later.

Merci à leurs auteurs. Ces outils conservent leur licence, ne sont pas inclus dans le dépôt, et s’installent séparément (voir [Installation](#installation)).

---

## Licence

[GNU GPL v3.0 ou ultérieure](LICENSE) © 2026 Michaël Bruzy.

`metmux` est un logiciel libre sous licence GPL-3.0-or-later : vous pouvez l’utiliser, l’étudier, le modifier et le redistribuer, y compris à des fins commerciales, à la seule condition que tout dérivé distribué reste lui aussi sous GPL, code source ouvert.
