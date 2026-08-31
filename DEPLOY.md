# 🚀 Hetzner VPS'e Dağıtım (Ubuntu)

Bu rehber projeyi Hetzner Cloud sunucunda Docker ile 7/24 çalıştırır.
Komutları sırayla SSH terminaline yapıştırabilirsin.

**Kurulan yapı:** tek imaj, iki container.

| Container | Görev | Port |
|---|---|---|
| `yatirimasistan-bot` | Motor: piyasayı tarar, al/sat kararı verir | – |
| `yatirimasistan-panel` | Streamlit paneli | 8501 |

İkisi `trading-data` adlı Docker volume'unu paylaşır; SQLite veritabanı orada
durur, container'ları silsen bile kaybolmaz.

> **Neden iki container?** Panel kendi içinde de motor çalıştırabiliyor
> (`RUN_BOT_IN_DASHBOARD`), ama compose kurulumunda panel bunu `false` yapar.
> Yoksa iki süreç aynı sinyalde iki kez pozisyon açardı.

---

## 1. Sunucuya bağlan

```bash
ssh root@SUNUCU_IP
```

Sistemi güncelle:

```bash
apt update && apt upgrade -y
```

---

## 2. Docker ve Docker Compose kurulumu

Ubuntu'nun kendi deposundaki paket eski olabiliyor; Docker'ın resmi deposunu kullan:

```bash
# Gerekli araçlar
apt install -y ca-certificates curl git

# Docker'ın GPG anahtarı
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# Depoyu ekle
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

# Kur
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Sunucu yeniden başlayınca Docker da otomatik açılsın
systemctl enable --now docker

# Doğrula
docker --version && docker compose version
```

---

## 3. Kodu sunucuya çek

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/nietzische-app/yatirimasistan-.git
cd yatirimasistan-

# Kod şu an feature branch'inde; main'e merge etmediysen:
git checkout claude/binance-paper-trading-bot-70l7jm
```

> Repo private ise: GitHub'da bir **deploy key** oluştur
> (`ssh-keygen -t ed25519 -C "hetzner" -f ~/.ssh/id_ed25519` → `cat ~/.ssh/id_ed25519.pub`
> çıktısını repo → Settings → Deploy keys'e ekle) ve SSH adresiyle klonla:
> `git clone git@github.com:nietzische-app/yatirimasistan-.git`

---

## 4. Ayar dosyasını oluştur

```bash
cp .env.example .env
nano .env          # kaydet: Ctrl+O, Enter, çık: Ctrl+X
chmod 600 .env     # API anahtarı koyacaksan şart
```

En azından şunu kontrol et:

```ini
DEMO_MODE=true             # gerçek para YOK
TZ=Europe/Istanbul
INITIAL_BALANCE=10000
```

`.env` dosyası `.gitignore` içinde — asla commit'lenmez.

---

## 5. Başlat

```bash
docker compose up -d --build
```

İlk build birkaç dakika sürer. Kontrol:

```bash
docker compose ps          # iki servis de "running" olmalı
docker compose logs -f     # canlı log (çıkmak için Ctrl+C)
docker compose logs -f bot # sadece motor logları
```

Panel ayakta mı:

```bash
curl -s http://127.0.0.1:8501/_stcore/health    # -> ok
```

`restart: always` sayesinde sunucu yeniden başlasa bile ikisi de otomatik kalkar.
Botun "çalışıyor" durumu veritabanında tutulduğu için, panelden bir kez
**Başlat**'a bastıysan reboot sonrası kendiliğinden taramaya devam eder.

---

## 6. Panele erişim

Varsayılan olarak panel **yalnızca sunucunun içinden** erişilebilir
(`127.0.0.1:8501`). Üç seçeneğin var:

### A) SSH tüneli — en güvenli, ekstra kurulum yok ✅

Kendi bilgisayarında (şirket bilgisayarında CMD/PowerShell de olur):

```bash
ssh -L 8501:localhost:8501 root@SUNUCU_IP
```

Bağlantı açıkken tarayıcıda: **http://localhost:8501**
Dışarıya hiçbir port açılmaz, şifre gerekmez, firewall ayarı gerekmez.

### B) Kullanıcı adı + şifre + HTTPS (Caddy) — kalıcı erişim için önerilir ✅

Panelin önüne [Caddy](https://caddyserver.com) ters vekil sunucusu konur: şifre
sorar, alan adın varsa sertifikayı da otomatik alır. Kurulum tek komut:

```bash
cd /opt/yatirimasistan-
./deploy/setup-caddy.sh
```

Script sırayla şunları sorar ve gerisini kendisi yapar:

| Soru | Ne olur |
|---|---|
| **Erişim modu** (1/2/3) | aşağıdaki tabloya bak |
| **Kullanıcı adı** | varsayılan `admin` |
| **Şifre** (2 kez, ekrana yazılmaz) | bcrypt hash'i üretilir; şifrenin kendisi hiçbir yere kaydedilmez |

Sonra `.env` dosyasını doğru biçimde yazar, hash'in Compose tarafından
bozulmadan okunduğunu **doğrular** ve servisleri Caddy ile başlatır.

#### Hangi modu seçmeli?

| Mod | Ne zaman | Sonuç |
|---|---|---|
| **1 — Alan adım var** | `panel.alanadin.com` gibi bir kaydı sunucunun IP'sine yönlendirdiysen | Let's Encrypt sertifikası otomatik alınır, gerçek HTTPS. **Önerilen.** Portlar: 80 + 443 |
| **2 — Sadece IP, HTTPS** | Alan adın yok ama trafiğin şifreli olsun istiyorsan | Self-signed sertifika: trafik şifreli, tarayıcı bir kez "güvenli değil" uyarısı verir ("Gelişmiş → Devam et") . Port: 443 |
| **3 — Sadece IP, düz HTTP** | Sadece geçici deneme | Şifre sorar ama **şifre ağda açık gider**. Kalıcı kullanma. Port: 80 |

> Mod 1 için alan adının A kaydı sunucunun IP'sine bakmalı ve 80/443 portları
> dışarıdan erişilebilir olmalı — Let's Encrypt doğrulaması bunu gerektirir.
> Ucuz bir alan adı, panelini kalıcı olarak güvene almanın en kolay yolu.

#### Elle yapmak istersen

```bash
# 1) Şifre hash'i (şifre komut geçmişine düşmesin diye stdin'den)
printf '%s\n' 'SIFREN' | docker run --rm -i caddy:2-alpine caddy hash-password

# 2) .env dosyasına ekle
nano .env
```

```ini
SITE_ADDRESS=panel.alanadin.com        # mod 2: https://SUNUCU_IP   | mod 3: :80
CADDY_TLS=                             # mod 2 ise: 'tls internal'
PANEL_USER=admin
PANEL_PASSWORD_HASH='$2a$14$....'      # ⚠️ TEK TIRNAK içinde yaz
```

```bash
chmod 600 .env
docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml up -d --build
```

> **Tek tırnak neden şart?** bcrypt hash'i `$` içerir ve Docker Compose `.env`
> içindeki `$` işaretlerini değişken sanıp hash'i kırpar (`$2a$14$abc` → `$2a$14`),
> sonuçta şifren hiçbir zaman doğrulanmaz. Tek tırnak bunu engeller. Kontrol:
>
> ```bash
> docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml config \
>   | grep PANEL_PASSWORD_HASH
> ```
>
> Çıktıdaki `$$` işaretleri normaldir (Compose'un kaçış biçimi); hash'in tamamı
> görünüyorsa doğrudur. `setup-caddy.sh` bu kontrolü senin için yapar.

#### Kurulumdan sonra

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml ps
docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml logs -f caddy
```

Tarayıcıdan adrese git; kullanıcı adı/şifre sorulmalı. Şifreyi değiştirmek
istersen `./deploy/setup-caddy.sh` scriptini tekrar çalıştırman yeterli.

Caddy'yi kullanırken **8501'i internete açma** (aşağıdaki C seçeneği); trafik
Caddy üzerinden 80/443 ile gelmeli, yoksa şifre atlanmış olur.

### C) 8501'i doğrudan internete açma — şifresiz ⚠️

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.public.yml up -d
```

Bu durumda **URL'yi bulan herkes** botu durdurabilir, başlatabilir ve bakiyeyi
sıfırlayabilir; Streamlit'in kendi şifre koruması yoktur. Bunu sadece
firewall'da kendi IP'ni whitelist'lersen kullan (aşağıya bak).

---

## 7. Firewall

### Hetzner Cloud Firewall (önerilen)

Sunucunun dışında, ağ seviyesinde çalışır — Docker'ın kurallarından etkilenmez.

1. [console.hetzner.cloud](https://console.hetzner.cloud) → projen → **Firewalls** → **Create Firewall**
2. **Inbound rules**:
   | Protokol | Port | Kaynak (Source) |
   |---|---|---|
   | TCP | 22 | kendi IP'n `x.x.x.x/32` |
   | TCP | 8501 | kendi IP'n `x.x.x.x/32` *(sadece C seçeneğinde)* |
   | TCP | 80, 443 | `0.0.0.0/0`, `::/0` *(Caddy — mod 1)* |
   | TCP | 443 | `0.0.0.0/0`, `::/0` *(Caddy — mod 2)* |
   | TCP | 80 | `0.0.0.0/0`, `::/0` *(Caddy — mod 3)* |
3. **Apply to Resources** → sunucunu seç → **Create Firewall**

Kendi IP'ni öğrenmek için: `curl ifconfig.me`

### UFW (sunucu içi)

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 8501/tcp        # sadece C seçeneğinde
ufw enable
ufw status verbose
```

> ### ⚠️ Bilinmesi şart: UFW, Docker'ın yayınladığı portları engellemez
>
> Docker port yayınlarken kuralları `DOCKER-USER`/nat zincirine yazar ve bu
> zincir UFW'nin `INPUT` kurallarından **önce** çalışır. Yani
> `ufw deny 8501` yazsan bile `ports: "8501:8501"` ile yayınlanmış panel
> internetten erişilebilir kalır. Bu yüzden:
>
> * Bu projede panel varsayılan olarak `127.0.0.1:8501` dinler (UFW'ye ihtiyaç duymaz), **veya**
> * Hetzner Cloud Firewall kullan (Docker'ı umursamaz), **veya**
> * Kuralı doğru zincire yaz:
>   ```bash
>   iptables -I DOCKER-USER ! -s SENIN_IP/32 -p tcp --dport 8501 -j DROP
>   apt install -y iptables-persistent && netfilter-persistent save
>   ```

---

## 8. Günlük kullanım

```bash
cd /opt/yatirimasistan-

docker compose ps                  # durum
docker compose logs -f bot         # canlı log
docker compose restart bot         # motoru yeniden başlat
docker compose down                # durdur (veri volume'da kalır)
docker compose up -d               # tekrar başlat
```

**Kodu güncelleme:**

```bash
cd /opt/yatirimasistan-
git pull
docker compose up -d --build
```

**Veritabanı yedeği:**

```bash
docker run --rm -v yatirimasistan_trading-data:/d -v $PWD:/b \
  alpine tar czf /b/yedek-$(date +%F).tgz -C /d .
```

**Yedekten geri dönme:**

```bash
docker compose down
docker run --rm -v yatirimasistan_trading-data:/d -v $PWD:/b \
  alpine sh -c "rm -rf /d/* && tar xzf /b/yedek-2026-01-01.tgz -C /d"
docker compose up -d
```

**Veritabanına doğrudan bakmak:**

```bash
docker compose exec dashboard python -c \
  "import database as db; print(db.get_stats())"
```

---

## 9. Gerçek moda geçiş (sonradan)

```bash
nano .env
```

```ini
DEMO_MODE=false
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
USE_TESTNET=true          # önce testnet ile dene!
```

```bash
chmod 600 .env
docker compose up -d       # container'lar yeni ayarla yeniden başlar
```

API anahtarını üretirken **sadece "Spot & Margin Trading"** yetkisi ver, para
çekme yetkisi verme ve Binance tarafında sunucunun IP'sini whitelist'le.

---

## 10. Sorun giderme

| Belirti | Kontrol / Çözüm |
|---|---|
| `docker compose up` build'de takılıyor | `docker compose build --no-cache` |
| Panel açılmıyor | `docker compose ps` → `dashboard` "running (healthy)" mi? `docker compose logs dashboard` |
| Bot işlem yapmıyor | Panelde **Başlat**'a bastın mı? `docker compose logs -f bot` ile RSI/EMA değerlerine bak — sinyal koşulu oluşmamış olabilir. |
| `NetworkError` / `HTTP 451` | Binance API sunucunun bulunduğu ülkede engelli olabilir. Hetzner Almanya lokasyonu genelde sorunsuzdur; değilse `EXCHANGE_ID=binanceus` veya başka bir borsa dene. |
| Saatler yanlış | `.env` içinde `TZ=Europe/Istanbul` ve `docker compose up -d` |
| Disk doluyor | Log rotasyonu compose'da tanımlı (10 MB × 5). Eski imajlar için: `docker image prune -a` |
| Baştan başlamak | Panelden "Sanal Bakiyeyi Sıfırla" ya da `docker compose exec bot python bot.py --reset` |
