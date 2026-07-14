# metmux - Intégrations gestionnaire de fichiers

Le moyen le plus simple d'utiliser `metmux`, et ce pour quoi il a été développé à la base, c'est de faire clic droit sur un fichier, sans avoir à passer par le terminal.

Compatible Windows, macOS et Linux.

> **Vous arrivez du [README principal](../README.fr.md) ?** Vous avez déjà tout. Passez directement à votre OS pour poursuivre l’installation.
>
> Prérequis, sinon :
> - Python 3.8+ et les moteurs (exiftool, ffmpeg, mutagen) installés — voir l'[Installation](../README.fr.md#installation) du README principal.
> - Le dépôt récupéré ([`metmux.py`](../metmux.py) **et** ce dossier [`integrations/`](../integrations/)) — voir [Récupérer le programme](../README.fr.md#récupérer-le-programme).

---

## Windows ⊞ — menu contextuel

Objectif : ajouter une ligne « Edit metadata » (« Éditer les métadonnées ») en faisant clic droit sur un fichier.

1. Déplacer le fichier [`metmux.py`](../metmux.py) dans un dossier stable (exemple : `C:\Tools\metmux\metmux.py`).
2. Ouvrir [`windows/metmux.bat`](windows/metmux.bat) dans un éditeur de texte.
3. À la ligne 5 (la ligne `set "METMUX=`), remplacer le chemin d’exemple (`C:\Tools\metmux\metmux.py`) par le vrai chemin où vous avez placé [`metmux.py`](../metmux.py) et enregistrer.
4. Déplacer [`metmux.bat`](windows/metmux.bat) dans le même dossier que [`metmux.py`](../metmux.py).
5. Ouvrir [`windows/metmux.reg`](windows/metmux.reg) dans un éditeur de texte.
6. À la dernière ligne, remplacer le chemin d’exemple (`C:\\Tools\\metmux\\metmux.bat`) par le vrai chemin. Attention : chaque antislash doit être doublé : `\` → `\\`.
> Pour que le nom soit en français, remplacer simplement, à la ligne 7, `@="Edit metadata"` par `@="Éditer les métadonnées"`.

7. Enregistrer, puis double-cliquer sur [`metmux.reg`](windows/metmux.reg). Windows demande de confirmer l’ajout au registre : confirmer.

→ C’est prêt. Clic droit sur un fichier affiche maintenant « Edit metadata ». Valider ouvre `metmux` dans le Cmd.
> Sur Windows 11, sous « Afficher plus d’options ».
>
> Lors de la sélection de plusieurs fichiers, Windows démarre en réalité un metmux par fichier ; metmux les fusionne ensuite de lui-même en une seule session.

Pour désinstaller, double-cliquer simplement sur [`windows/metmux_uninstall.reg`](windows/metmux_uninstall.reg).

---

## macOS 🍎 — Action rapide (Finder)

Objectif : ajouter une ligne « Edit metadata » en faisant clic droit sur un fichier dans le Finder via ce que macOS appelle une « Action rapide » (*Quick Action*).

### Méthode 1 : automatique
1. Clic droit sur [`macos/Edit metadata`](macos/Edit%20metadata.workflow), choisir « Ouvrir avec » puis « Programme d’installation Automator ».
2. Cliquer sur « Installer ».
3. L’action attend [`metmux.py`](../metmux.py) dans `~/metmux/metmux.py` (le dépôt à la racine de votre dossier personnel). Placé ailleurs ? Aller dans `~/Library/Services` (dossier caché : dans le Finder, menu Aller → « Aller au dossier… », coller `~/Library/Services`), puis clic droit sur `Edit metadata`, « Ouvrir avec » puis « Automator ». Corriger la ligne `METMUX=` avec votre vrai chemin.
> Pour que le nom soit en français, renommer simplement le fichier `Edit metadata` en `Éditer les métadonnées`, ce qui aura pour effet de changer son nom au clic droit.

### Méthode 2 : manuelle

1. Ouvrir Automator.
2. Choisir **Nouveau document** tout en bas à gauche, et choisir **Action rapide**.
3. En haut de la fenêtre, régler « Le processus reçoit l’élément actuel » sur « fichiers ou dossiers » dans « Finder ».
4. Dans la colonne gauche, chercher l’action « Exécuter un script Shell » et double-cliquer dessus (ou glisser dans la zone de droite).
5. Régler dans le cadre :
   	- Shell : /bin/bash
   	- Données en entrée : comme arguments
6. Effacer le contenu par défaut dans la zone de script, et coller tout le contenu de [`macos/metmux_quickaction.sh`](macos/metmux_quickaction.sh).
7. À la ligne 5 (la ligne `METMUX=`), remplacer le chemin d’exemple (`$HOME/metmux/metmux.py`) par le vrai chemin où vous avez placé [`metmux.py`](../metmux.py) et enregistrer.
8. Enregistrer (Cmd ⌘ + S) en choisissant le nom « Edit metadata » ou « Éditer les métadonnées » par exemple.

→ C’est prêt. Clic droit sur un fichier dans le Finder affiche maintenant « Actions rapides » puis l’action `Edit metadata` ou tout autre nom choisi. Valider ouvre `metmux` dans le Terminal.

> Au premier lancement, macOS demande d’autoriser le contrôle de « Terminal » : acceptez. Un refus rend ensuite l’action silencieusement inopérante (réparable dans Réglages Système → Confidentialité et sécurité → Automatisation).

---

## Linux 🐧 — menu contextuel

### Nautilus (GNOME), Nemo, Caja

Objectif : ajouter une ligne « Edit metadata » au menu **Scripts** du clic droit.

1. Ouvrir [`linux/Edit metadata`](linux/Edit%20metadata) dans un éditeur de texte.
2. À la ligne 5 (la ligne `METMUX=`), remplacer le chemin d’exemple (`$HOME/metmux/metmux.py`) par le vrai chemin où vous avez placé [`metmux.py`](../metmux.py) et enregistrer.
3. Copier ce script dans le dossier des scripts de votre gestionnaire de fichiers, puis le rendre exécutable. Les commandes ci-dessous se lancent **depuis le dossier du dépôt** (celui qui contient `metmux.py` et `integrations/`) : ailleurs, remplacer `integrations/linux/Edit metadata` par le chemin complet de ce fichier.

   Pour Nautilus (GNOME) :
   ```sh
   mkdir -p ~/.local/share/nautilus/scripts
   cp "integrations/linux/Edit metadata" ~/.local/share/nautilus/scripts/
   chmod +x ~/.local/share/nautilus/scripts/"Edit metadata"
   ```
   Pour Nemo (Cinnamon, Linux Mint) :
   ```sh
   mkdir -p ~/.local/share/nemo/scripts
   cp "integrations/linux/Edit metadata" ~/.local/share/nemo/scripts/
   chmod +x ~/.local/share/nemo/scripts/"Edit metadata"
   ```
   Pour Caja (MATE) :
   ```sh
   mkdir -p ~/.config/caja/scripts
   cp "integrations/linux/Edit metadata" ~/.config/caja/scripts/
   chmod +x ~/.config/caja/scripts/"Edit metadata"
   ```

→ C’est prêt. Clic droit sur un fichier affiche maintenant Scripts, puis « Edit metadata ». Valider ouvre `metmux` dans la console.

> Pour que le nom soit en français, renommer simplement le script copié `Edit metadata` en `Éditer les métadonnées`, ce qui aura pour effet de changer son nom au clic droit.

### Autre méthode : « Ouvrir avec » (tous les bureaux)

Si vous préférez, ou si votre gestionnaire de fichiers n’a pas de menu Scripts (Dolphin sur KDE/Plasma, Thunar sur Xfce), le fichier [`linux/metmux.desktop`](linux/metmux.desktop) ajoute « Éditer les métadonnées » au menu **Ouvrir avec**.

1. Ouvrir [`linux/metmux.desktop`](linux/metmux.desktop) dans un éditeur de texte.
2. À la ligne 9 (la ligne `Exec=`), remplacer le chemin d’exemple (`/home/USER/metmux/metmux.py`) par le vrai chemin où vous avez placé [`metmux.py`](../metmux.py), et enregistrer.
3. Exécuter cette commande, depuis le dossier du dépôt :
   ```sh
   cp integrations/linux/metmux.desktop ~/.local/share/applications/
   ```
→ C’est prêt. Clic droit sur un fichier affiche maintenant « Ouvrir avec » puis « Éditer les métadonnées ». Selon le bureau, la première fois, l’entrée peut n’apparaître que sous « Autre application… » : choisissez-la une fois et elle remonte ensuite dans la liste.
