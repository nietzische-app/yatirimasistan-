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

# TradingAgents submodule'ünü indir (ZORUNLU — imaj bunsuz build olmaz)
git submodule update --init --recursive
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
| **1 — Kendi alan adım var** | `panel.alanadin.com` kaydını sunucunun IP'sine yönlendirdiysen | Let's Encrypt sertifikası otomatik alınır, gerçek HTTPS. Portlar: 80 + 443 |
| **2 — Alan adım yok (sslip.io)** | Alan adı satın almadıysan | IP'den ücretsiz bir alan adı türetilir (`panel-5-9-1-2.sslip.io`) ve **gerçek** Let's Encrypt sertifikası alınır — tarayıcı uyarısı yok. **Önerilen.** Portlar: 80 + 443 |
| **3 — Sadece IP, HTTPS** | sslip.io gibi bir servise bağımlı olmak istemiyorsan | Self-signed sertifika: trafik şifreli, tarayıcı bir kez "güvenli değil" uyarısı verir. Port: 443 |
| **4 — Sadece IP, düz HTTP** | Sadece geçici deneme | Şifre sorar ama **şifre ağda açık gider**. Kalıcı kullanma. Port: 80 |

> **sslip.io nedir?** `5-9-1-2.sslip.io` gibi adları `5.9.1.2` IP'sine çözen
> ücretsiz bir joker DNS servisidir; kayıt olmak veya DNS kaydı açmak gerekmez.
> Gerçek bir alan adı olduğu için Let's Encrypt sertifika verebilir. Karşılığında
> adresin okunabilirliği düşük olur ve dışarıdaki bir servise bağımlı kalırsın;
> kalıcı bir kurulum için ucuz bir alan adı yine de en iyisi.

> Mod 1 ve 2'de 80/443 portları dışarıdan erişilebilir olmalı — Let's Encrypt
> doğrulaması bunu gerektirir.

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

#### Sunucuda zaten bir web sunucusu varsa (80/443 dolu)

Kurulum şu hatayı verirse sunucuda başka bir servis o portu tutuyordur:

```
Error response from daemon: ... Bind for 0.0.0.0:80 failed: port is already allocated
```

Kimin tuttuğunu gör:

```bash
ss -tlnp | grep -E ':80 |:443 '
docker ps --format '{{.Names}}\t{{.Ports}}'
```

Doğru çözüm ne bulduğuna bağlı:

| Ne çalışıyor | Ne yapmalı |
|---|---|
| **Bir vekil sunucu container'ı** (Caddy, Traefik, nginx-proxy...) | İkinci bir vekil sunucu çalıştırma; paneli mevcut olana tanıt: `./deploy/setup-existing-caddy.sh` |
| **Doğrudan sunucuda nginx/apache** | `deploy/nginx-panel.conf.example` ile mevcut nginx'e ekle (aşağıda) |
| Alakasız bir uygulama (ör. 80'i kullanan başka bir container) | `./deploy/setup-caddy.sh` — çakışmayı görüp boş bir port seçer |

---

##### Zaten bir Caddy container'ın varsa (en yaygın durum)

```bash
cd /opt/yatirimasistan-
./deploy/setup-existing-caddy.sh
```

Script sırayla:

1. 80/443'ü yayınlayan container'ı bulur (birden fazlaysa sorar)
2. O container'ın Docker ağını bulur ve `PROXY_NETWORK` olarak `.env`'e yazar
3. Paneli o ağa da bağlar — `deploy/docker-compose.external-proxy.yml` overlay'i
   ile; panel `127.0.0.1:8501`'de dinlemeye de devam eder (SSH tüneli bozulmaz)
4. Panelin adresini sorar — **alan adın yoksa sorun değil:**
   - Kendi alan adın varsa onu yaz
   - Yoksa **sslip.io** seçeneğini seç: IP'nden `panel-5-9-1-2.sslip.io` gibi
     ücretsiz bir alan adı türetir ve gerçek Let's Encrypt sertifikası alınır
   - Ya da düz IP + self-signed (tarayıcı bir kez uyarır)

   Ardından kullanıcı adı ve şifre sorar, bcrypt hash'ini üretir
5. `docker exec` ile mevcut Caddy'nin paneli gerçekten görüp göremediğini test eder
6. Caddyfile'a eklenecek bloğu yazar; onay verirsen **yedek alıp** ekler,
   `caddy validate` ile doğrular ve ancak geçerliyse `caddy reload` yapar.
   Doğrulama başarısızsa yedeği geri yükler ve çalışan Caddy'ye hiç dokunmaz.

Elle yapmak istersen: `deploy/existing-caddy-site.Caddyfile.example` dosyasındaki
bloğu kendi Caddyfile'ına ekle, `PROXY_NETWORK=<ağ adı>` satırını `.env`'e yaz ve:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.external-proxy.yml up -d
docker exec <caddy-container> caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec <caddy-container> caddy reload   --config /etc/caddy/Caddyfile
```

> Panel container'ı mevcut Caddy ile **aynı Docker ağına** bağlandığı için
> `reverse_proxy yatirimasistan-panel:8501` adıyla erişilir. Panel host'ta
> yalnızca `127.0.0.1`'e bağlı olduğundan, ağa bağlanmadan `172.17.0.1:8501`
> gibi bir adresle erişmeye çalışmak işe yaramaz.

---

##### Doğrudan sunucuda nginx varsa

Panel zaten `127.0.0.1:8501` adresinde dinliyor, yani Caddy'ye hiç gerek yok:

```bash
# Şifre dosyası
apt install -y apache2-utils
htpasswd -c /etc/nginx/.htpasswd-panel admin      # şifreyi sorar

# Site tanımı
cp deploy/nginx-panel.conf.example /etc/nginx/sites-available/panel.conf
nano /etc/nginx/sites-available/panel.conf        # server_name ve sertifika yollarını düzenle
ln -s /etc/nginx/sites-available/panel.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

Örnek dosya `deploy/nginx-panel.conf.example` içinde; Streamlit'in WebSocket
bağlantısı için gereken `Upgrade`/`Connection` başlıkları ve uzun
`proxy_read_timeout` değeri ayarlanmış durumda — bunlar olmadan panel açılır
ama "Connection error" verip donar.

Bu yolda Caddy'yi hiç başlatma; sade `docker compose up -d` yeterli.

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

docker compose exec bot python bot.py --status    # tek bakışta her şey
docker compose exec bot python bot.py --list-models deepseek  # model ID'lerini bul
docker compose exec bot python bot.py --test-llm  # model kurul için uygun mu
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
| Build'de `trading_agents` bulunamıyor | `git submodule update --init --recursive` çalıştır |
| Panelde "Kurul devre dışı: LLM anahtarı yok" | `.env` içine `OPENROUTER_API_KEY` ekle, `docker compose up -d` |
| Alpaca emirleri reddediliyor | Panelde "Reddedilen emirler" bölümüne bak; genelde alım gücü yetersiz ya da hisse için borsa kapalı |
| Kurul hep ERROR veriyor | `docker compose logs bot \| grep -i agent` — çoğunlukla kota (429) veya model adı hatası |
| `Bind for 0.0.0.0:80 failed: port is already allocated` | Başka bir servis 80'i tutuyor. `ss -tlnp \| grep ':80 '` ile bak; yukarıdaki "Sunucuda zaten bir web sunucusu varsa" bölümünü uygula. |
| Panel açılıyor ama "Connection error" verip donuyor | Ters vekil sunucu WebSocket'i geçirmiyor. nginx'te `Upgrade`/`Connection` başlıklarını ve `proxy_read_timeout 86400;` satırını ekle. |
| Panel açılmıyor | `docker compose ps` → `dashboard` "running (healthy)" mi? `docker compose logs dashboard` |
| Bot işlem yapmıyor | Panelde **Başlat**'a bastın mı? `docker compose logs -f bot` ile RSI/EMA değerlerine bak — sinyal koşulu oluşmamış olabilir. |
| `NetworkError` / `HTTP 451` | Binance API sunucunun bulunduğu ülkede engelli olabilir. Hetzner Almanya lokasyonu genelde sorunsuzdur; değilse `EXCHANGE_ID=binanceus` veya başka bir borsa dene. |
| Saatler yanlış | `.env` içinde `TZ=Europe/Istanbul` ve `docker compose up -d` |
| Disk doluyor | Log rotasyonu compose'da tanımlı (10 MB × 5). Eski imajlar için: `docker image prune -a` |
| Baştan başlamak | Panelden "Sanal Bakiyeyi Sıfırla" ya da `docker compose exec bot python bot.py --reset` |
