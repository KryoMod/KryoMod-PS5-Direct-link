# dlpsgame-pegasus-scraper

GitHub Action qui scrape quotidiennement **[dlpsgame.com/category/ps5/](https://dlpsgame.com/category/ps5/)**, extrait les **liens de téléchargement directs** (en résolvant les redirections `downloadgameps3.net` vers `akirabox.com`, `vikingfile.com`, `datanodes.to`, etc.), et produit un fichier **`dlpsgame-ps5.json`** au **format catalogue Pegasus DL**.

Le JSON de sortie est directement consommable par **[pegasus-ps5/pegasus-dl](https://github.com/pegasus-ps5/pegasus-dl)** (onglet *Sources* → *Add URL*) et par le script `https://pippo26442999.github.io/.exFAT/script.js` qui utilise exactement la même structure (`titleId`, `title`, `version`, `category`, `posterUrl`, `description`, `downloadLinks[].name`, `downloadLinks[].url`, `sizeBytes`).

---

## ⚠️ Important : Cloudflare + GitHub Actions

Le site `dlpsgame.com` est protégé par **Cloudflare** avec challenge JS + validation TLS fingerprint (JA3/JA4). Les IP datacenter des runners GitHub Actions sont bloquées (HTTP 403).

**On ne peut pas** récupérer les cookies `cf_clearance` via FlareSolverr puis les réutiliser avec `curl`, car Cloudflare valide aussi l'empreinte TLS : `curl` a une signature TLS différente de Chrome, donc la requête est rejetée (403) même avec les bons cookies + User-Agent.

**Solution :** Le workflow GitHub Action lance **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** en tant que service Docker, et **TOUTES les requêtes** du scraper passent par FlareSolverr via une **session persistante** (une instance Chrome reste ouverte avec les cookies Cloudflare valides pendant tout le scrape).

| Backend | Usage | Vitesse |
|---|---|---|
| `curl` | **Local** (votre machine, IP résidentielle non bloquée par CF) | ⚡ Rapide (0.5s/req) |
| `flaresolverr` | **GitHub Actions** / IP bloquées — toutes les req via FlareSolverr + session persistante | 🐢 ~2-3s/req |

**Le workflow GitHub Action utilise `flaresolverr` par défaut.**

---

## 📁 Structure du dépôt

```
.
├── .github/
│   └── workflows/
│       └── scrape.yml              ← GitHub Action (cron quotidien + manuel)
├── scripts/
│   └── scrape_dlpsgame.py          ← Le scraper Python
├── requirements.txt                ← Dépendances Python (beautifulsoup4)
├── README.md
├── dlpsgame-ps5.example.json       ← Exemple de sortie
└── dlpsgame-ps5.json               ← Généré automatiquement chaque jour
```

---

## 🚀 Mise en place

### 1. Créez un nouveau dépôt GitHub

```bash
# Exemple : mon-utilisateur/dlpsgame-ps5-catalog
```

### 2. Poussez les fichiers de ce dossier dans le dépôt

```bash
git init
git add .
git commit -m "feat: scraper dlpsgame PS5 + GitHub Action quotidienne"
git branch -M main
git remote add origin https://github.com/VOTRE_UTILISATEUR/dlpsgame-ps5-catalog.git
git push -u origin main
```

### 3. Lancez le premier scrape manuellement

Dans l'onglet **Actions** → **Scrape dlpsgame PS5 catalog** → **Run workflow**.

Le workflow va :
1. Démarrer FlareSolverr en tant que service Docker (health check automatique)
2. Attendre que FlareSolverr soit prêt (jusqu'à 5 minutes)
3. Tester la résolution Cloudflare sur `dlpsgame.com/category/ps5/`
4. Lancer le scraper Python en mode `flaresolverr` avec session persistante
5. Comitter le fichier `dlpsgame-ps5.json` à la racine du dépôt

### 4. Vérifiez le résultat

- Téléchargez l'artifact `dlpsgame-ps5-catalog` depuis le run
- Ou regardez le fichier `dlpsgame-ps5.json` committé à la racine du dépôt

---

## ⏰ Planification

Le workflow tourne automatiquement **tous les jours à 04:00 UTC** (06:00 en heure de Paris en été, 05:00 en hiver).

Vous pouvez le lancer manuellement à tout moment via **Run workflow**, avec des paramètres optionnels :
- `max_pages` : limite le nombre de pages de catégorie parcourues
- `max_games` : limite le nombre de jeux scrapés

La concurrency est forcée à **1** en mode FlareSolverr (une session = un Chrome = pas de parallélisme).

---

## 📦 Format du JSON produit

```json
{
  "name": "dlpsgame PS5",
  "version": 1,
  "generatedAt": "2026-06-21T18:33:56+00:00",
  "source": "https://dlpsgame.com/category/ps5/",
  "packages": [
    {
      "titleId": "PPSA01968",
      "title": "Death Stranding Directors Cut",
      "version": "01.004",
      "category": "game",
      "posterUrl": "https://dlpsgame.com/wp-content/uploads/2026/06/1-fjhg-1.jpg",
      "description": "Tags: PPSA01968, v1.004, 2.xx, EUR\nSize: 54GB\nCredits: Pippo26442999 x SiESPTA\nThanks: SiESPTA for the Files and drakmor for ampr-emu\nFW: 2.xx and beyond",
      "downloadLinks": [
        { "name": "Rootz",     "url": "https://www.rootz.so/d/9WKsC" },
        { "name": "Mediafire", "url": "https://www.mediafire.com/file/..." },
        { "name": "Akia",      "url": "https://akirabox.com/jar3Xak6Nz2d/file" },
        { "name": "Viki",      "url": "https://vikingfile.com/f/sjFAw5MGf1" },
        { "name": "1File",     "url": "https://1fichier.com/?vem83fu0q3fnzg8861sr" },
        { "name": "Buzz",      "url": "https://buzzheavier.com/0u3r65w1uk4k" },
        { "name": "Data",      "url": "https://datanodes.to/z5wv842jnrn8" },
        { "name": "Filek",     "url": "https://filekeeper.net/q8qt9gp28xfq" },
        { "name": "Vault",     "url": "https://datavaults.co/1zbpn8x8imgr" }
      ],
      "sizeBytes": 57982058496,
      "downloadSource": "https://dlpsgame.com/death-stranding-directors-cut-ps5/"
    }
  ]
}
```

### Champs

| Champ | Description |
|---|---|
| `name` | Nom du catalogue (constant : "dlpsgame PS5") |
| `version` | Version du format (constant : 1) |
| `generatedAt` | Timestamp ISO-8601 du scrape |
| `source` | URL de la catégorie source |
| `packages[].titleId` | ID PS5 au format `PPSAxxxxx` (extrait des tags) |
| `packages[].title` | Titre du jeu |
| `packages[].version` | Version du dump (ex: `01.004`) |
| `packages[].category` | Toujours `"game"` |
| `packages[].posterUrl` | URL de la cover/poster (og:image de la page) |
| `packages[].description` | Tags + Size + Credits + Thanks + FW |
| `packages[].downloadLinks[].name` | Nom du miroir (Akia, Viki, Data, Filek, Vault, Rootz, Mediafire, 1File, Buzz, ...) |
| `packages[].downloadLinks[].url` | **URL directe** vers l'hébergeur (les redirections `downloadgameps3.net` sont résolues) |
| `packages[].sizeBytes` | Taille en octets (parsée depuis "54GB" → 57982058496) |
| `packages[].downloadSource` | URL de la page de jeu sur dlpsgame.com |

### Miroirs reconnus

| Pattern dans l'URL | Nom affiché |
|---|---|
| `akirabox` | Akia |
| `vikingfile` | Viki |
| `datanodes.to` | Data |
| `filekeeper` | Filek |
| `datavaults` | Vault |
| `buzzheavier` | Buzz |
| `1fichier` | 1File |
| `mediafire` | Mediafire |
| `rootz.so` | Rootz |
| `gofile` | Gofile |
| `1cloudfile` | 1Cloud |
| `mega.nz` | Mega |

---

## 🔧 Comment ça marche

### Architecture (mode FlareSolverr)

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  scrape.py      │────▶│  FlareSolverr    │────▶│  dlpsgame.com   │
│  (Python)       │     │  (Docker, Chrome)│     │  (Cloudflare)   │
│                 │     │                  │     │                 │
│  1. Crée session│     │  Garde Chrome    │     │  Challenge JS   │
│     persistante │────▶│  ouvert avec     │     │  résolu ✓       │
│                 │     │  cookies CF      │     │                 │
│  2. Toutes les  │     │                  │     │                 │
│     requêtes    │────▶│  request.get     │────▶│  200 OK         │
│     passent par │◀────│  (réutilise      │◀────│  HTML complet   │
│     FlareSolverr│     │   la session)    │     │                 │
│                 │     │  ~2-3s/req       │     │                 │
│  3. Détruit la  │────▶│  sessions.destroy│     │                 │
│     session     │     │  (ferme Chrome)  │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

1. **Init** : Le scraper crée une session FlareSolverr persistante (`sessions.create`). FlareSolverr garde une instance Chrome ouverte avec les cookies `cf_clearance` valides.
2. **Pré-chargement** : Une première requête sur `dlpsgame.com/category/ps5/` résout le challenge Cloudflare et stocke les cookies dans la session.
3. **Scraping** : Toutes les requêtes suivantes (pages de catégorie, pages de jeu, résolution `downloadgameps3.net`) passent par FlareSolverr en réutilisant la session. Chaque requête prend ~2-3 secondes.
4. **Nettoyage** : À la fin, le scraper détruit la session (`sessions.destroy`) pour fermer proprement Chrome.

### Découverte des jeux

Le scraper parcourt `https://dlpsgame.com/category/ps5/page/N` en partant de N=1 et en incrémentant tant que des liens `*-ps5/` sont trouvés.

### Extraction des liens sécurisés

Chaque page de jeu contient un ou plusieurs `<div class="secure-data" data-payload="BASE64">`. Le scraper :

1. Décode le base64 du `data-payload` → HTML en clair
2. Parse le HTML avec BeautifulSoup
3. Extrait tous les `<a href>` (en ignorant Guide/Tool/DMCA/Download/Here/...)
4. Pour chaque lien, détecte le miroir à partir du texte (Akia, Viki, ...) ou de l'URL

### Résolution des redirections `downloadgameps3.net`

Les liens qui passent par `downloadgameps3.net/archives/{id}` ne sont PAS des redirections HTTP classiques : c'est une page HTML avec des liens obfqués en JavaScript via deux attributs `data-domain` + `data-path` :

```html
<a class="secure-lnk"
   data-domain="https://akirabox."
   data-path="com/obj3jd07rGDx/file">Akia</a>
```

Le scraper :

1. Fetch la page `downloadgameps3.net/archives/{id}` via FlareSolverr
2. Parse tous les `<a class="secure-lnk">`
3. Reconstruit l'URL finale en concaténant `data-domain` + `data-path` → `https://akirabox.com/obj3jd07rGDx/file`
4. Si `mirror_hint` est fourni (Akia/Viki/...), on privilégie le lien qui matche

### Déduplication

Les liens sont dédupliqués par URL finale. Si deux spoilers (régulier + exFAT) pointent vers le même `downloadgameps3.net/archives/{id}`, on ne garde qu'une seule entrée.

---

## 🧪 Tester localement

### En mode `curl` (si votre IP n'est pas bloquée par Cloudflare)

```bash
pip install -r requirements.txt
python scripts/scrape_dlpsgame.py --http-backend curl --out dlpsgame-ps5.json
```

### En mode `flaresolverr` (recommandé, avec Docker)

```bash
# 1. Lancez FlareSolverr en Docker
docker run -d --name flaresolverr -p 8191:8191 \
  ghcr.io/flaresolverr/flaresolverr:latest

# 2. Lancez le scraper
pip install -r requirements.txt
python scripts/scrape_dlpsgame.py \
  --http-backend flaresolverr \
  --flaresolverr-url http://localhost:8191/v1 \
  --out dlpsgame-ps5.json
```

### Test rapide (3 jeux seulement)

```bash
python scripts/scrape_dlpsgame.py \
  --max-pages 1 --max-games 3 \
  --http-backend curl \
  --verbose
```

---

## ⏱️ Performance attendue

| Mode | Temps par requête | ~100 jeux (200 req) | Concurrency |
|---|---|---|---|
| `curl` (local) | ~0.5s | ~2 minutes | 2-4 threads |
| `flaresolverr` (GitHub Actions) | ~2-3s | ~7-10 minutes | 1 (séquentiel) |

Le workflow a un timeout de **120 minutes**, largement suffisant.

---

## ⚠️ Notes légales

- Ce scraper ne fait que **lire des pages publiques** et **résoudre des liens**.
- Il ne télécharge, ne stocke et ne redistribue **aucun fichier copyrighté**.
- Le JSON produit ne contient que des **métadonnées** (titre, taille, cover) et des **liens** vers des hébergeurs tiers.
- À vous de vérifier la légalité de l'usage dans votre juridiction.

---

## 🐛 Dépannage

| Symptôme | Solution |
|---|---|
| **HTTP 403 sur dlpsgame.com** | Vous êtes sur une IP bloquée par Cloudflare. Utilisez `--http-backend flaresolverr` avec FlareSolverr en Docker. |
| **FlareSolverr injoignable** | Lancez-le : `docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest` |
| **"Challenge Cloudflare NON résolu"** | Vérifiez les logs FlareSolverr : `docker logs flaresolverr`. Mettez à jour l'image : `docker pull ghcr.io/flaresolverr/flaresolverr:latest`. |
| HTTP 429 sur `downloadgameps3.net` | Le scraper retry automatiquement (5 fois avec backoff 5s→120s). |
| Workflow ne se déclenche pas | GitHub peut retarder les cron. Pour un scrape immédiat, utilisez **Run workflow**. |
| Workflow ne commit pas | Vérifiez que `permissions: contents: write` est bien dans `scrape.yml`. |
| Peu de jeux scrapés | Regardez `dlpsgame-ps5.warnings.txt` pour les pages en échec. |
| FlareSolverr health check fail | Augmentez `--health-retries` dans le workflow (défaut: 10). |

---

## 📜 Licence

MIT. Utilisez librement.
