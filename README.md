# Téléchargeur web yt-dlp

## Démarrage
    cp .env.example .env
    docker compose up -d --build
    docker compose logs -f updater

L'interface écoute sur 0.0.0.0:8083

## Mise à jour automatique de yt-dlp
Le service `updater` vérifie la dernière release GitHub toutes les
`UPDATE_INTERVAL_HOURS` heures, vérifie le SHA-256, teste le binaire puis le
remplace atomiquement. Les tâches suivantes utilisent la nouvelle version
sans redémarrage. `YTDLP_CHANNEL` : stable, nightly ou master.
Forcer une vérification : `docker compose restart updater`.

## Cookies (vidéos privées, Captcha)
Exportez un cookies.txt (format Netscape) et placez-le dans ./config/cookies.txt.

## Nginx
    server {
        listen 443 ssl http2;
        server_name dl.example.fr;
        # ssl_certificate ... ;
        client_max_body_size 1m;
        location / {
            proxy_pass http://127.0.0.1:8083;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_buffering off;
            proxy_read_timeout 600s;
        }
    }

## Mettre à jour les images (Python, ffmpeg, Deno)
    docker compose build --pull && docker compose up -d
