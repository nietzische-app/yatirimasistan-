#!/usr/bin/env bash
#
# Paneli kullanıcı adı/şifre (ve mümkünse HTTPS) arkasına alır.
#
# Kullanım (proje kökünde):
#   ./deploy/setup-caddy.sh
#
# Yaptıkları:
#   1. Erişim modunu sorar (alan adı / IP+self-signed / düz HTTP)
#   2. Kullanıcı adı ve şifreyi sorar, bcrypt hash'ini üretir
#      (şifre ekrana yazılmaz, komut geçmişine ve process listesine düşmez)
#   3. .env dosyasına doğru biçimde yazar (bcrypt'teki $ işaretleri için tek tırnak)
#   4. Hash'in Compose tarafından bozulmadan okunduğunu doğrular
#   5. Servisleri Caddy overlay'i ile başlatır

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_FILE=".env"
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml)

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# --- .env satırını ekle/güncelle (sed kullanılmaz: bcrypt hash'i $ ve / içerir) ---
set_env() {
  local key="$1" val="$2" tmp found=0
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" == "$key="* ]]; then
        printf '%s=%s\n' "$key" "$val" >> "$tmp"
        found=1
      else
        printf '%s\n' "$line" >> "$tmp"
      fi
    done < "$ENV_FILE"
  fi
  [ "$found" = 1 ] || printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# --- Bir portu kim tutuyor? (boşsa çıktı yok, dönüş 1) --------------------
# Sırayla: docker -> ss -> netstat -> lsof -> bind denemesi.
port_owner() {
  local port="$1" out

  # 1) Docker container'ı mı yayınlıyor? (en anlaşılır cevap)
  out="$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null \
         | awk -v p=":${port}->" 'index($0, p) { print $1; exit }')"
  if [ -n "$out" ]; then printf 'docker container "%s"' "$out"; return 0; fi

  # 2) Süreç adını verebilen araçlar
  if command -v ss >/dev/null 2>&1; then
    out="$(ss -tlnp 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p { print; exit }' \
           | grep -o '"[^"]*"' | head -1 | tr -d '"')"
  elif command -v netstat >/dev/null 2>&1; then
    out="$(netstat -tlnp 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p { print $NF; exit }' \
           | cut -d/ -f2)"
  elif command -v lsof >/dev/null 2>&1; then
    out="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -F c 2>/dev/null \
           | awk '/^c/ { print substr($0, 2); exit }')"
  fi
  if [ -n "${out:-}" ]; then printf '%s' "$out"; return 0; fi

  # 3) Hiçbir araç yoksa: porta bağlanmayı dene
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$port" <<'PYEOF' && return 1 || { printf 'bilinmeyen bir servis'; return 0; }
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)      # port dolu
finally:
    s.close()
PYEOF
  fi
  return 1
}

port_busy() { port_owner "$1" >/dev/null; }

# Verilen adaylardan ilk boş portu yaz
first_free_port() {
  local p
  for p in "$@"; do port_busy "$p" || { printf '%s' "$p"; return 0; }; done
  return 1
}

# --- Ön kontroller ---
command -v docker >/dev/null || { red "docker bulunamadı. Önce DEPLOY.md bölüm 2'yi uygula."; exit 1; }
docker compose version >/dev/null 2>&1 || { red "docker compose eklentisi yok. DEPLOY.md bölüm 2."; exit 1; }
[ -f "$ENV_FILE" ] || { cp .env.example "$ENV_FILE"; grn ".env oluşturuldu (.env.example kopyalandı)"; }

bold "Panel erişim modu"
cat <<'MENU'
  1) Alan adım var          -> otomatik HTTPS (Let's Encrypt). ÖNERİLEN.
                               Alan adının A kaydı sunucunun IP'sine bakmalı,
                               80 ve 443 portları dışarı açık olmalı.
  2) Sadece IP + HTTPS      -> self-signed sertifika. Trafik şifreli ama
                               tarayıcı "güvenli değil" uyarısı verir (1 kez onaylarsın).
  3) Sadece IP + düz HTTP   -> şifre sorar ama trafik şifresizdir; şifren
                               ağda açık gider. Sadece geçici kullan.
MENU
read -rp "Seçim [1-3]: " MODE

case "$MODE" in
  1)
    read -rp "Alan adı (örn. panel.alanadin.com): " DOMAIN
    [ -n "$DOMAIN" ] || { red "Alan adı boş olamaz."; exit 1; }
    SITE_ADDRESS="$DOMAIN"; CADDY_TLS=""; URL="https://$DOMAIN"
    PORTS="80 ve 443"
    ;;
  2)
    read -rp "Sunucu IP adresi: " IP
    [ -n "$IP" ] || { red "IP boş olamaz."; exit 1; }
    SITE_ADDRESS="https://$IP"; CADDY_TLS="tls internal"; URL="https://$IP"
    PORTS="443"
    ;;
  3)
    read -rp "Sunucu IP adresi: " IP
    [ -n "$IP" ] || { red "IP boş olamaz."; exit 1; }
    SITE_ADDRESS=":80"; CADDY_TLS=""; URL="http://$IP"
    PORTS="80"
    ;;
  *) red "Geçersiz seçim."; exit 1 ;;
esac

# --- Port çakışması kontrolü -------------------------------------------------
# Sunucuda başka bir web sunucusu (nginx, apache, başka bir Docker yığını...)
# 80/443'ü kullanıyorsa Caddy başlayamaz:
#   "Bind for 0.0.0.0:80 failed: port is already allocated"
# Compose her iki portu da yayınladığı için ikisini de kontrol etmek gerekir.
CADDY_HTTP_PORT=80
CADDY_HTTPS_PORT=443

# Panele erişilecek asıl port moda göre değişir
if [ "$MODE" = "3" ]; then PRIMARY=http; else PRIMARY=https; fi

# --- 1) Asıl port ---
if [ "$PRIMARY" = "https" ] && OWNER="$(port_owner 443)"; then
  if [ "$MODE" = "1" ]; then
    red "443 portunu $OWNER kullanıyor."
    red "Let's Encrypt sertifikası 443 üzerinden doğrulanır; bu port başka bir"
    red "servisteyken alan adı modu kurulamaz."
    echo
    echo "Seçeneklerin:"
    echo "  * O servis bir ters vekil sunucu ise (nginx/apache/traefik), ikinci bir"
    echo "    vekil sunucu çalıştırma; paneli ona tanıt."
    echo "    -> DEPLOY.md 'Sunucuda zaten bir web sunucusu varsa' bölümü"
    echo "       (hazır örnek: deploy/nginx-panel.conf.example)"
    echo "  * Ya da bu scripti 2. modla (IP + self-signed) çalıştır; farklı bir port seçer."
    exit 1
  fi
  ALT="$(first_free_port 8443 9443 10443 || true)"
  read -rp "443 portunu $OWNER kullanıyor. Panel hangi portta olsun? [${ALT:-8443}]: " ANS
  CADDY_HTTPS_PORT="${ANS:-${ALT:-8443}}"
  URL="${URL}:${CADDY_HTTPS_PORT}"
  PORTS="$CADDY_HTTPS_PORT"
elif [ "$PRIMARY" = "http" ] && OWNER="$(port_owner 80)"; then
  ALT="$(first_free_port 8080 9080 10080 || true)"
  read -rp "80 portunu $OWNER kullanıyor. Panel hangi portta olsun? [${ALT:-8080}]: " ANS
  CADDY_HTTP_PORT="${ANS:-${ALT:-8080}}"
  URL="${URL}:${CADDY_HTTP_PORT}"
  PORTS="$CADDY_HTTP_PORT"
fi

# --- 2) İkincil port (Compose onu da yayınlar; dolu olması kurulumu bozar) ---
if [ "$PRIMARY" = "https" ]; then
  # 80 sadece http->https yönlendirmesi için; taşınabilir.
  if OWNER="$(port_owner "$CADDY_HTTP_PORT")"; then
    ALT="$(first_free_port 8080 9080 10080 18080 || true)"
    if [ -z "$ALT" ]; then
      read -rp "80 portunu $OWNER kullanıyor, alternatif bulunamadı. Hangi port? [18080]: " ALT
      ALT="${ALT:-18080}"
    fi
    CADDY_HTTP_PORT="$ALT"
    [ "$MODE" = "1" ] && PORTS="443"
    echo "Not: 80 portunu $OWNER kullanıyor; Caddy onun yerine $CADDY_HTTP_PORT portunu alacak."
    echo "     Sonuç: http:// adresi otomatik https:// ye yönlenmez; adresi https:// ile yaz."
    [ "$MODE" = "1" ] && echo "     Sertifika 443 üzerinden (TLS-ALPN) alınacağı için bu sorun olmaz."
  fi
else
  # Mod 3'te HTTPS portu kullanılmaz ama Compose yine de yayınlar.
  if OWNER="$(port_owner "$CADDY_HTTPS_PORT")"; then
    ALT="$(first_free_port 8443 9443 10443 18443 || true)"
    if [ -z "$ALT" ]; then
      read -rp "443 portunu $OWNER kullanıyor, alternatif bulunamadı. Hangi port? [18443]: " ALT
      ALT="${ALT:-18443}"
    fi
    CADDY_HTTPS_PORT="$ALT"
    echo "Not: 443 portunu $OWNER kullanıyor; Caddy o eşlemeyi $CADDY_HTTPS_PORT üzerinden yapacak"
    echo "     (bu modda kullanılmıyor, sadece çakışmayı önlemek için)."
  fi
fi

read -rp "Panel kullanıcı adı [admin]: " PANEL_USER
PANEL_USER="${PANEL_USER:-admin}"

read -rsp "Panel şifresi (en az 8 karakter): " PASS; echo
read -rsp "Şifre (tekrar): " PASS2; echo
[ "$PASS" = "$PASS2" ] || { red "Şifreler eşleşmiyor."; exit 1; }
[ "${#PASS}" -ge 8 ]   || { red "Şifre en az 8 karakter olmalı."; exit 1; }

bold "Şifre hash'i üretiliyor (caddy imajı indiriliyor olabilir)..."
# Şifre stdin'den geçer; komut satırında/process listesinde görünmez.
HASH="$(printf '%s\n' "$PASS" | docker run --rm -i caddy:2-alpine caddy hash-password)"
unset PASS PASS2
case "$HASH" in
  \$2*) ;;
  *) red "Hash üretilemedi. Çıktı: $HASH"; exit 1 ;;
esac

# bcrypt hash'i $ içerir -> Compose değişken sanmasın diye TEK TIRNAK şart.
set_env SITE_ADDRESS "$SITE_ADDRESS"
set_env CADDY_TLS "'$CADDY_TLS'"
set_env PANEL_USER "$PANEL_USER"
set_env CADDY_HTTP_PORT "$CADDY_HTTP_PORT"
set_env CADDY_HTTPS_PORT "$CADDY_HTTPS_PORT"
set_env PANEL_PASSWORD_HASH "'$HASH'"
chmod 600 "$ENV_FILE"
grn ".env güncellendi (izinler 600)"

# --- Hash Compose'dan bozulmadan geçiyor mu? ---
bold "Yapılandırma doğrulanıyor..."
RESOLVED="$("${COMPOSE[@]}" config 2>/dev/null \
  | grep -m1 'PANEL_PASSWORD_HASH' \
  | sed -e 's/.*PANEL_PASSWORD_HASH: *//' -e 's/^"//' -e 's/"$//')"
EXPECTED="${HASH//\$/\$\$}"        # compose config çıktısında $ -> $$
if [ "$RESOLVED" != "$EXPECTED" ]; then
  red "Hash Compose tarafından bozulmuş görünüyor."
  red "  beklenen: $EXPECTED"
  red "  okunan  : $RESOLVED"
  red ".env içindeki PANEL_PASSWORD_HASH satırını tek tırnak içine al ve tekrar dene."
  exit 1
fi
grn "Hash doğru okunuyor."

bold "Servisler başlatılıyor..."
"${COMPOSE[@]}" up -d --build

echo
grn "Kurulum tamam."
echo "  Adres        : $URL"
echo "  Kullanıcı    : $PANEL_USER"
echo "  Açık olması gereken portlar: $PORTS"
echo
echo "Sonraki adımlar:"
echo "  * Hetzner Cloud Firewall'da $PORTS portunu aç (DEPLOY.md bölüm 7)."
[ "$MODE" = "3" ] && echo "  * ⚠️  Trafik şifresiz; kalıcı kullanım için alan adı alıp 1. seçeneğe geç."
[ "$MODE" = "2" ] && echo "  * Tarayıcı sertifika uyarısı verecek; 'Gelişmiş -> Devam et' ile geç."
echo "  * Logları izle:  ${COMPOSE[*]} logs -f caddy"
