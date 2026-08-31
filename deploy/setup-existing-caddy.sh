#!/usr/bin/env bash
#
# Sunucuda ZATEN çalışan bir Caddy container'ı varsa paneli ona tanıtır.
# (İkinci bir Caddy çalıştırmaz — 80/443 zaten dolu olduğu için çalışamaz da.)
#
# Kullanım (proje kökünde):
#   ./deploy/setup-existing-caddy.sh
#
# Yaptıkları:
#   1. Mevcut Caddy container'ını ve Docker ağını bulur
#   2. Panel container'ını o ağa bağlar (docker-compose.external-proxy.yml)
#   3. Kullanıcı adı/şifre sorar, bcrypt hash'ini üretir
#   4. Caddy'nin paneli görüp görmediğini test eder
#   5. Caddyfile'a eklenecek bloğu yazar; istersen yedek alıp kendisi ekler,
#      önce "caddy validate" ile doğrular, hata varsa yedeği geri yükler

set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_FILE=".env"
PANEL_CONTAINER="yatirimasistan-panel"
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.external-proxy.yml)

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

set_env() {
  local key="$1" val="$2" tmp found=0
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" == "$key="* ]]; then
        printf '%s=%s\n' "$key" "$val" >> "$tmp"; found=1
      else
        printf '%s\n' "$line" >> "$tmp"
      fi
    done < "$ENV_FILE"
  fi
  [ "$found" = 1 ] || printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
}

command -v docker >/dev/null || { red "docker bulunamadı."; exit 1; }
[ -f "$ENV_FILE" ] || cp .env.example "$ENV_FILE"

# --- 1) Mevcut vekil sunucu container'ı ------------------------------------
bold "80/443 portlarını tutan container aranıyor..."
mapfile -t CANDIDATES < <(docker ps --format '{{.Names}}\t{{.Ports}}' \
  | awk '/:80->|:443->/ { print $1 }' | grep -v "^${PANEL_CONTAINER}$" || true)

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
  red "80/443'ü yayınlayan bir container bulunamadı."
  echo "Vekil sunucun container değilse (doğrudan sunucuda nginx gibi):"
  echo "  -> deploy/nginx-panel.conf.example ve DEPLOY.md'ye bak."
  echo "Hiç vekil sunucun yoksa:"
  echo "  -> ./deploy/setup-caddy.sh (kendi Caddy'sini kurar)"
  exit 1
elif [ "${#CANDIDATES[@]}" -eq 1 ]; then
  PROXY_CONTAINER="${CANDIDATES[0]}"
  echo "Bulundu: $PROXY_CONTAINER"
else
  printf '%s\n' "${CANDIDATES[@]}" | nl -w2 -s') '
  read -rp "Hangisi? [1]: " N; N="${N:-1}"
  PROXY_CONTAINER="${CANDIDATES[$((N-1))]}"
fi

# --- 2) Ağını bul -----------------------------------------------------------
mapfile -t NETS < <(docker inspect "$PROXY_CONTAINER" \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | sed '/^$/d')
if [ "${#NETS[@]}" -eq 0 ]; then
  red "$PROXY_CONTAINER hiçbir ağa bağlı değil (network_mode: host olabilir)."
  echo "Bu durumda panel'i 127.0.0.1:8501 üzerinden vekil sunucuya tanıt."
  exit 1
elif [ "${#NETS[@]}" -eq 1 ]; then
  PROXY_NETWORK="${NETS[0]}"
else
  printf '%s\n' "${NETS[@]}" | nl -w2 -s') '
  read -rp "Panel hangi ağa bağlansın? [1]: " N; N="${N:-1}"
  PROXY_NETWORK="${NETS[$((N-1))]}"
fi
echo "Ağ: $PROXY_NETWORK"
set_env PROXY_NETWORK "$PROXY_NETWORK"

# --- 3) Panelin adresi ------------------------------------------------------
bold "Panele hangi adresten erişeceksin?"
cat <<'MENU'
  1) Kendi alan adım var        -> panel.alanadin.com gibi. Gerçek HTTPS.
  2) Alan adım yok (sslip.io)   -> IP'den otomatik bir alan adı türetilir
                                   (ör. 5-9-1-2.sslip.io). Ücretsiz, kayıt
                                   gerekmez, Let's Encrypt sertifikası alınır.
                                   ÖNERİLEN.
  3) Alan adım yok (düz IP)     -> https://SUNUCU_IP, self-signed sertifika.
                                   Trafik şifreli ama tarayıcı uyarı verir.
                                   DİKKAT: IP'ye bağlanırken tarayıcı SNI
                                   göndermediği için bazı Caddy kurulumlarında
                                   el sıkışma hiç kurulmuyor
                                   (ERR_SSL_PROTOCOL_ERROR). Olmazsa 2'yi seç.
MENU
read -rp "Seçim [1-3]: " ADDR_MODE
SITE_TLS=""

# Sunucunun genel IP'sini tespit edip varsayılan olarak öner
ask_ip() {
  local d=""
  # 1) Sunucunun kendi arayüzündeki IP (Hetzner'de bu zaten genel IP'dir)
  if command -v ip >/dev/null 2>&1; then
    d="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)"
  fi
  # 2) Doğrulama/yedek: dış servisler (NAT arkasındaysan doğrusunu bunlar verir)
  if [ -z "$d" ]; then
    for u in https://ifconfig.me https://api.ipify.org https://icanhazip.com; do
      d="$(curl -fsS --max-time 8 "$u" 2>/dev/null | tr -d '[:space:]')"
      [ -n "$d" ] && break
    done
  fi
  read -rp "Sunucunun genel IP'si [${d:-}]: " IP
  IP="${IP:-$d}"
  [ -n "$IP" ] || { red "IP boş olamaz."; exit 1; }
}

case "$ADDR_MODE" in
  1)
    read -rp "Alan adı (örn. panel.alanadin.com): " PANEL_DOMAIN
    [ -n "$PANEL_DOMAIN" ] || { red "Alan adı boş olamaz."; exit 1; }
    SITE_ADDR="$PANEL_DOMAIN"
    ;;
  2)
    ask_ip
    case "$IP" in
      *[!0-9.]*|"") red "Geçerli bir IPv4 adresi gir (ör. 5.9.1.2)."; exit 1 ;;
    esac
    PANEL_DOMAIN="panel-${IP//./-}.sslip.io"
    SITE_ADDR="$PANEL_DOMAIN"
    echo "Kullanılacak alan adı: $PANEL_DOMAIN"
    echo "(sslip.io bu adı $IP adresine çözer; ayrıca bir DNS kaydı gerekmez.)"
    ;;
  3)
    ask_ip
    PANEL_DOMAIN="$IP"
    SITE_ADDR="https://$IP"
    SITE_TLS=$'\n\ttls internal'
    ;;
  *) red "Geçersiz seçim."; exit 1 ;;
esac

read -rp "Panel kullanıcı adı [admin]: " PANEL_USER; PANEL_USER="${PANEL_USER:-admin}"
read -rsp "Panel şifresi (en az 8 karakter): " PASS; echo
read -rsp "Şifre (tekrar): " PASS2; echo
[ "$PASS" = "$PASS2" ] || { red "Şifreler eşleşmiyor."; exit 1; }
[ "${#PASS}" -ge 8 ]   || { red "Şifre en az 8 karakter olmalı."; exit 1; }

bold "Şifre hash'i üretiliyor..."
HASH="$(printf '%s\n' "$PASS" | docker run --rm -i caddy:2-alpine caddy hash-password)"
unset PASS PASS2
case "$HASH" in \$2*) ;; *) red "Hash üretilemedi: $HASH"; exit 1 ;; esac

# --- 4) Paneli o ağa bağla --------------------------------------------------
bold "Panel container'ı $PROXY_NETWORK ağına bağlanıyor..."
"${COMPOSE[@]}" up -d

# Streamlit'in ayağa kalkması ~10 sn sürer; tek seferlik kontrol erken kalır.
bold "Caddy paneli görebiliyor mu? (panelin açılması bekleniyor)"
REACHABLE=0
for i in $(seq 1 20); do
  if docker exec "$PROXY_CONTAINER" sh -c \
       "wget -qO- http://${PANEL_CONTAINER}:8501/_stcore/health 2>/dev/null || \
        curl -fsS http://${PANEL_CONTAINER}:8501/_stcore/health 2>/dev/null" 2>/dev/null | grep -q ok; then
    REACHABLE=1
    break
  fi
  printf '.'
  sleep 3
done
echo
if [ "$REACHABLE" = "1" ]; then
  grn "Evet: $PROXY_CONTAINER -> $PANEL_CONTAINER:8501 erişimi çalışıyor (${i}. denemede)."
else
  red "Hayır: $PROXY_CONTAINER container'ı 60 saniye boyunca $PANEL_CONTAINER:8501"
  red "adresine ulaşamadı."
  echo
  echo "Sırayla şunlara bak:"
  echo "  1) Panel ayakta mı?    docker compose ps"
  echo "     Panel logu:         docker compose logs --tail=30 dashboard"
  echo "  2) Aynı ağda mı?"
  echo "     docker inspect $PANEL_CONTAINER --format '{{range \$k,\$v := .NetworkSettings.Networks}}{{\$k}} {{end}}'"
  echo "     docker inspect $PROXY_CONTAINER --format '{{range \$k,\$v := .NetworkSettings.Networks}}{{\$k}} {{end}}'"
  echo "     İkisinde de '$PROXY_NETWORK' görünmeli."
  echo "  3) İsim çözülüyor mu?"
  echo "     docker exec $PROXY_CONTAINER nslookup $PANEL_CONTAINER"
  echo
  echo "Bloğu yine de ekleyebilirsin: blok doğru, erişim düzelince Caddy'yi"
  echo "yeniden yüklemeye bile gerek kalmaz (upstream her istekte çözülür)."
fi

# --- 5) Caddyfile bloğu -----------------------------------------------------
SNIPPET_FILE="$(mktemp)"
cat > "$SNIPPET_FILE" <<EOF

# --- Yatirim Asistani paneli (yatirimasistan-) ---
${SITE_ADDR} {${SITE_TLS}
	basic_auth {
		$PANEL_USER $HASH
	}
	reverse_proxy ${PANEL_CONTAINER}:8501
}
EOF

echo
bold "Caddyfile'ına eklenecek blok:"
cat "$SNIPPET_FILE"

# Caddyfile'ı mount'lardan bul
CADDYFILE_HOST="$(docker inspect "$PROXY_CONTAINER" \
  --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')"

if [ -z "$CADDYFILE_HOST" ] || [ ! -f "$CADDYFILE_HOST" ]; then
  echo
  echo "Caddyfile otomatik bulunamadı. Kendin ekle, sonra:"
  echo "  docker exec $PROXY_CONTAINER caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
  echo "  docker exec $PROXY_CONTAINER caddy reload   --config /etc/caddy/Caddyfile"
  rm -f "$SNIPPET_FILE"
  exit 0
fi

echo
echo "Caddyfile bulundu: $CADDYFILE_HOST"
read -rp "Bu bloğu dosyaya ekleyip Caddy'yi yeniden yükleyeyim mi? [e/H]: " ANS
case "${ANS:-h}" in
  [eEyY]*) ;;
  *) echo "Tamam, elle ekleyebilirsin."; rm -f "$SNIPPET_FILE"; exit 0 ;;
esac

BACKUP="${CADDYFILE_HOST}.bak-$(date +%Y%m%d-%H%M%S)"
cp "$CADDYFILE_HOST" "$BACKUP"
grn "Yedek alındı: $BACKUP"
cat "$SNIPPET_FILE" >> "$CADDYFILE_HOST"
rm -f "$SNIPPET_FILE"

bold "Yapılandırma doğrulanıyor..."
if ! docker exec "$PROXY_CONTAINER" caddy validate \
      --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
  red "Doğrulama başarısız! Yedek geri yükleniyor, Caddy'ye dokunulmadı."
  cp "$BACKUP" "$CADDYFILE_HOST"
  docker exec "$PROXY_CONTAINER" caddy validate \
    --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -5
  exit 1
fi
grn "Doğrulama tamam."

bold "Caddy yeniden yükleniyor..."
if docker exec "$PROXY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  grn "Yüklendi."
else
  red "Reload başarısız. Yedek geri yükleniyor: $BACKUP"
  cp "$BACKUP" "$CADDYFILE_HOST"
  docker exec "$PROXY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile || true
  exit 1
fi

echo
grn "Kurulum tamam."
echo "  Adres     : https://$PANEL_DOMAIN"
if [ "$ADDR_MODE" = "3" ]; then
  echo "  Not       : Tarayıcı sertifika uyarısı verecek; 'Gelişmiş -> Devam et'."
  echo "              Uyarı yerine ERR_SSL_PROTOCOL_ERROR görürsen Caddy o IP için"
  echo "              sertifika üretememiştir. Kontrol:  docker logs --tail=40 $PROXY_CONTAINER"
  echo "              Çözüm: yedeği geri yükleyip scripti 2. modla (sslip.io) çalıştır:"
  echo "                cp $BACKUP $CADDYFILE_HOST"
  echo "                docker exec $PROXY_CONTAINER caddy reload --config /etc/caddy/Caddyfile"
  echo "                ./deploy/setup-existing-caddy.sh"
fi
[ "$ADDR_MODE" = "2" ] && echo "  Not       : Adres sslip.io üzerinden IP'ne çözülür; sertifika gerçektir."
echo "  Kullanıcı : $PANEL_USER"
echo "  Yedek     : $BACKUP"
echo
echo "Sertifika ilk isteğe kadar birkaç saniye sürebilir. Log:"
echo "  docker logs -f $PROXY_CONTAINER"
