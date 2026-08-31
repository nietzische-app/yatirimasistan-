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
